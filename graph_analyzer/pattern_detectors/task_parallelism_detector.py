# This file is part of the DiscoPoP software (http://www.discopop.tu-darmstadt.de)
#
# Copyright (c) 2019, Technische Universitaet Darmstadt, Germany
#
# This software may be modified and distributed under the terms of
# a BSD-style license.  See the LICENSE file in the package base
# directory for details.


from typing import List

from graph_tool import Vertex

import PETGraph
from pattern_detectors.PatternInfo import PatternInfo
from utils import find_subnodes, depends, calculate_workload, \
    total_instructions_count, classify_task_vars

__forks = set()
__workloadThreshold = 10000
__minParallelism = 3


class Task(object):
    """This class represents task in task parallelism pattern
    """
    nodes: List[Vertex]
    child_tasks: List['Task']
    start_line: str
    end_line: str

    def __init__(self, pet: PETGraph, node: Vertex):
        self.node_id = pet.graph.vp.id[node]
        self.nodes = [node]
        self.start_line = pet.graph.vp.startsAtLine[node]
        if ":" in self.start_line:
            self.region_start_line = self.start_line[self.start_line.index(":") + 1 :]
        else:
            self.region_start_line = self.start_line
        self.region_end_line = None
        self.end_line = pet.graph.vp.endsAtLine[node]
        self.mw_type = pet.graph.vp.mwType[node]
        self.instruction_count = total_instructions_count(pet, node)
        self.workload = calculate_workload(pet, node)
        self.child_tasks = []

    def aggregate(self, other: 'Task'):
        """Aggregates given task into current task

        :param other: task to aggregate
        """
        self.nodes.extend(other.nodes)
        self.end_line = other.end_line
        self.workload += other.workload
        self.instruction_count += other.instruction_count
        self.mw_type = 'BARRIER_WORKER' if other.mw_type == 'BARRIER_WORKER' else 'WORKER'


def __merge_tasks(pet: PETGraph, task: Task):
    """Merges the tasks into having required workload.

    :param pet: PET graph
    :param task: task node
    """
    for i in range(len(task.child_tasks)):
        child_task: Task = task.child_tasks[i]
        if child_task.workload < __workloadThreshold:  # todo child child_tasks?
            if i > 0:
                pred: Task = task.child_tasks[i - 1]
                if __neighbours(pred, child_task):
                    pred.aggregate(child_task)
                    pred.child_tasks.remove(child_task)
                    __merge_tasks(pet, task)
                    return
            if i + 1 < len(task.child_tasks) - 1:  # todo off by one?, elif?
                succ: Task = task.child_tasks[i + 1]
                if __neighbours(child_task, succ):
                    child_task.aggregate(succ)  # todo odd aggregation in c++
                    task.child_tasks.remove(succ)
                    __merge_tasks(pet, task)
                    return
            task.child_tasks.remove(child_task)
            __merge_tasks(pet, task)
            return

    if task.child_tasks and len(task.child_tasks) < __minParallelism:
        max_workload_task = max(task.child_tasks, key=lambda t: t.workload)
        task.child_tasks.extend(max_workload_task.child_tasks)
        task.child_tasks.remove(max_workload_task)
        __merge_tasks(pet, task)
        return

    for child in task.child_tasks:
        if pet.graph.vp.type[child.nodes[0]] == 'loop':
            pass  # todo add loops?


def __neighbours(first: Task, second: Task):
    """Checks if second task immediately follows first task

    :param first: predecessor task
    :param second: successor task
    :return: true if second task immediately follows first task
    """
    fel = int(first.end_line.split(':')[1])
    ssl = int(second.start_line.split(':')[1])
    return fel == ssl or fel + 1 == ssl or fel + 2 == ssl


class TaskParallelismInfo(PatternInfo):
    """Class, that contains task parallelism detection result
    """

    def __init__(self, pet: PETGraph, node: Vertex, pragma, pragma_line, first_private, private, shared):
        """
        :param pet: PET graph
        :param node: node, where task parallelism was detected
        :param pragma: pragma to be used (task / taskwait)
        :param pragma_line: line prior to which the pragma shall be inserted
        :param first_private: list of varNames
        :param private: list of varNames
        :param shared: list of varNames
        """
        PatternInfo.__init__(self, pet, node)
        self.pragma = pragma
        self.pragma_line = pragma_line
        if ":" in self.pragma_line:
            self.region_start_line = self.pragma_line[self.pragma_line.index(":")+1:]
        else:
            self.region_start_line = self.pragma_line
        self.region_end_line = None
        self.first_private = first_private
        self.private = private
        self.shared = shared

    def __str__(self):
        return f'Task parallelism at CU: {self.node_id}\n' \
               f'CU Start line: {self.start_line}\n' \
               f'CU End line: {self.end_line}\n' \
               f'pragma at line: {self.pragma_line}\n' \
               f'pragma region start line: {self.region_start_line}\n' \
               f'pragma region end line: {self.region_end_line}\n' \
               f'pragma: "#pragma omp {" ".join(self.pragma)}"\n' \
               f'first_private: {" ".join(self.first_private)}\n' \
               f'private: {" ".join(self.private)}\n' \
               f'shared: {" ".join(self.shared)}'


class ParallelRegionInfo(PatternInfo):
    """Class, that contains parallel region info.
    """
    def __init__(self, pet: PETGraph, node: Vertex, region_start_line, region_end_line):
        PatternInfo.__init__(self, pet, node)
        self.region_start_line = region_start_line
        self.region_end_line = region_end_line

    def __str__(self):
        return f'Task Parallel Region at CU: {self.node_id}\n' \
               f'CU Start line: {self.start_line}\n' \
               f'CU End line: {self.end_line}\n' \
               f'pragma: \n\t#pragma omp parallel\n\t#pragma omp single\n' \
               f'Parallel Region Start line: {self.region_start_line}\n' \
               f'Parallel Region End line {self.region_end_line}\n'


def run_detection(pet: PETGraph) -> List[TaskParallelismInfo]:
    """Computes the Task Parallelism Pattern for a node:
    (Automatic Parallel Pattern Detection in the Algorithm Structure Design Space p.46)
    1.) first merge all children of the node -> all children nodes get the dependencies
        of their children nodes and the list of the children nodes (saved in node.childrenNodes)
    2.) To detect Task Parallelism, we use Breadth First Search (BFS)
        a.) the hotspot becomes a fork
        b.) all child nodes become first worker if they are not marked as worker before
        c.) if a child has dependence to more than one parent node, it will be marked as barrier
    3.) if two barriers can run in parallel they are marked as barrierWorkers.
        Two barriers can run in parallel if there is not a directed path from one to the other

        :param pet: PET graph
        :return: List of detected pattern info
    """
    result = []

    for node in pet.graph.vertices():
        if pet.graph.vp.type[node] == 'dummy':
            continue
        if find_subnodes(pet, node, 'child'):
            # print(graph.vp.id[node])
            __detect_mw_types(pet, node)

        if pet.graph.vp.mwType[node] == 'NONE':
            pet.graph.vp.mwType[node] = 'ROOT'

    __forks.clear()
    __create_task_tree(pet, pet.main)

    # ct = [graph.vp.id[v] for v in pet.graph.vp.childrenTasks[main_node]]
    # ctt = [graph.vp.id[v] for v in forks]
    fs = [f for f in __forks if f.node_id == '130:0']
    for fork in fs:
        # todo __merge_tasks(graph, fork)
        if fork.child_tasks:
            result.append(TaskParallelismInfo(pet, fork.nodes[0], [], [], [], [], []))

    result += __detect_task_suggestions(pet)
    result = __remove_useless_barrier_suggestions(pet, result)
    result += __suggest_parallel_regions(pet, result)
    result = __set_task_contained_lines(pet, result)
    result = __detect_taskloop_reduction(pet, result)
    result = __detect_barrier_suggestions(pet, result)

    # TODO: data sharing protection clauses (including omittable)
    # TODO: combine omittable with tasks

    return result


def __detect_task_suggestions(pet: PETGraph):
    """creates task parallelism suggestions and returns them as a list of
    TaskParallelismInfo objects.
    Currently relies on previous processing steps and suggests WORKER CUs
    as Tasks and BARRIER/BARRIER_WORKER as Taskwaits.

    :param pet: PET graph
    :return List[TaskParallelismInfo]
    """
    # suggestions contains a map from LID to a set of suggestions. This is required to
    # detect multiple suggestions for a single line of source code.
    suggestions = dict() # LID -> List[TaskParallelismInfo]

    # get a list of cus classified as WORKER
    worker_cus = []
    barrier_cus = []
    barrier_worker_cus = []

    for v in pet.graph.vertices():
        if pet.graph.vp.mwType[v] == "WORKER":
            worker_cus.append(v)
        if pet.graph.vp.mwType[v] == "BARRIER":
            barrier_cus.append(v)
        if pet.graph.vp.mwType[v] == "BARRIER_WORKER":
            barrier_worker_cus.append(v)
    worker_cus = worker_cus + barrier_worker_cus

    # SUGGEST TASKWAIT
    for v in barrier_cus:
        # get line number of first dependency. suggest taskwait prior to that
        first_dependency_line = pet.graph.vp.endsAtLine[v]
        first_dependency_line_number = first_dependency_line[
            first_dependency_line.index(":") + 1:]
        for e in v.out_edges():
            if pet.graph.ep.type[e] == "dependence":
                dep_line = pet.graph.ep.sink[e]
                dep_line_number = dep_line[dep_line.index(":") + 1:]
                if dep_line_number < first_dependency_line_number:
                    first_dependency_line = dep_line
        tmp_suggestion = TaskParallelismInfo(pet, v, ["taskwait"],
                                             first_dependency_line,
                                             [], [], [])
        if pet.graph.vp.startsAtLine[v] not in suggestions:
            # no entry for source code line contained in suggestions
            tmp_set = []
            suggestions[pet.graph.vp.startsAtLine[v]] = tmp_set
            suggestions[pet.graph.vp.startsAtLine[v]].append(tmp_suggestion)
        else:
            # entry for source code line already contained in suggestions
            suggestions[pet.graph.vp.startsAtLine[v]].append(tmp_suggestion)

    # SUGGEST TASKS
    for vx in pet.graph.vertices():
        # iterate over all entries in recursiveFunctionCalls
        # in order to find task suggestions
        for i in range(0, len(pet.graph.vp.recursiveFunctionCalls[vx])):
            function_call_string = pet.graph.vp.recursiveFunctionCalls[vx][i]
            if not type(function_call_string) == str:
                continue
            contained_in = __recursive_function_call_contained_in_worker_cu(
                pet, function_call_string, worker_cus)
            if contained_in is not None:
                current_suggestions = None
                # recursive Function call contained in worker cu
                # -> issue task suggestion
                pragma_line = function_call_string[
                              function_call_string.index(":") + 1:]
                pragma_line = pragma_line.replace(",", "").replace(" ", "")

                # only include cu and func nodes
                if not ('func' in pet.graph.vp.type[contained_in] or
                        "cu" in pet.graph.vp.type[contained_in]):
                    continue

                if pet.graph.vp.mwType[contained_in] == "WORKER" or \
                        pet.graph.vp.mwType[contained_in] == "BARRIER_WORKER":
                    # suggest task
                    fpriv, priv, shared, in_dep, out_dep, in_out_dep, red = \
                        classify_task_vars(pet, contained_in, "", [], [])
                    current_suggestions = TaskParallelismInfo(pet, vx, ["task"],
                                            pragma_line,
                                            [v.name for v in fpriv],
                                            [v.name for v in priv],
                                            [v.name for v in shared])

                # insert current_suggestions into suggestions
                # check, if current_suggestions contains an element
                if current_suggestions is not None:
                    # current_suggestions contains something
                    if pragma_line not in suggestions:
                        # LID not contained in suggestions
                        tmp_set = []
                        suggestions[pragma_line] = tmp_set
                        suggestions[pragma_line].append(current_suggestions)
                    else:
                        # LID already contained in suggestions
                        suggestions[pragma_line].append(current_suggestions)
    # end of for loop

    # construct return value (list of TaskParallelismInfo)
    result = []
    for key in suggestions:
        for single_suggestion in suggestions[key]:
            result.append(single_suggestion)
    return result


def __detect_barrier_suggestions(pet: PETGraph,
                                 suggestions: [TaskParallelismInfo]):
    """detect barriers which have not been detected by __detect_mw_types,
    especially marks WORKER as BARRIER_WORKER if it has depencies to two or
    more CUs which are contained in a path to a CU containing at least one
    suggested Task.
    function executed is repeated until convergence.
    steps:
    1.) mark node as Barrier, if dependences only to task-containing-paths
    """
    # split suggestions into task and taskwait suggestions
    taskwait_suggestions = []
    task_suggestions = []
    for single_suggestion in suggestions:
        if type(single_suggestion) == ParallelRegionInfo:
            continue
        if single_suggestion.pragma[0] == "taskwait":
            taskwait_suggestions.append(single_suggestion)
        else:
            task_suggestions.append(single_suggestion)
    for s in task_suggestions:
        pet.graph.vp.viz_contains_task[s._node] = 'True'
    for s in taskwait_suggestions:
        pet.graph.vp.viz_contains_taskwait[s._node] = 'True'
    task_nodes = [t._node for t in task_suggestions]
    barrier_nodes = [t._node for t in taskwait_suggestions]

    transformation_happened = True
    # let run until convergence
    while transformation_happened:
        transformation_happened = False
        for v in pet.graph.vertices():
            # check step 1
            out_dep_edges = [e for e in v.out_edges() if
                             pet.graph.ep.type[e] == "dependence"]
            v_first_line = pet.graph.vp.startsAtLine[v]
            v_first_line = v_first_line[v_first_line.index(":") + 1:]
            task_count = 0
            barrier_count = 0
            normal_count = 0
            for e in out_dep_edges:
                if e.target() in task_nodes:
                    task_count += 1
                elif e.target() in barrier_nodes:
                    barrier_count += 1
                else:
                    normal_count += 1
            if task_count == 1 and barrier_count == 0:
                if pet.graph.vp.viz_omittable[v] == 'False':
                    #actual change
                    pet.graph.vp.viz_omittable[v] = 'True'
                    transformation_happened = True
            elif barrier_count != 0 and task_count != 0:
                # check if child barrier(s) cover each child task
                child_barriers = [e.target() for e in out_dep_edges if
                                  pet.graph.vp.viz_contains_taskwait[e.target()] ==
                                  'True']
                child_tasks = [e.target() for e in out_dep_edges if
                               pet.graph.vp.viz_contains_task[e.target()] ==
                               'True']
                uncovered_task_exists = False
                for ct in child_tasks:
                    ct_start_line = pet.graph.vp.startsAtLine[ct]
                    ct_start_line = ct_start_line[ct_start_line.index(":") + 1:]
                    ct_end_line = pet.graph.vp.endsAtLine[ct]
                    ct_end_line = ct_end_line[ct_end_line.index(":") + 1:]
                    # check if ct covered by a barrier
                    for cb in child_barriers:
                        cb_start_line = pet.graph.vp.startsAtLine[cb]
                        cb_start_line = cb_start_line[cb_start_line.index(":") + 1:]
                        cb_end_line = pet.graph.vp.endsAtLine[cb]
                        cb_end_line = cb_end_line[cb_end_line.index(":") + 1:]
                        if not (cb_start_line > ct_start_line and
                                cb_end_line > ct_end_line):
                            uncovered_task_exists = True
                if uncovered_task_exists:
                    # suggest barrier
                    if pet.graph.vp.viz_contains_taskwait[v] == 'False':
                        # actual change
                        pet.graph.vp.viz_contains_taskwait[v] = 'True'
                        barrier_nodes.append(v)
                        transformation_happened = True
                        tmp_suggestion = TaskParallelismInfo(pet, v,
                                                             ["taskwait"],
                                                             v_first_line,
                                                             [], [], [])
                        suggestions.append(tmp_suggestion)
                else:
                    # no barrier needed
                    pass
            elif task_count != 0:
                if pet.graph.vp.viz_contains_taskwait[v] == 'False':
                    # actual change
                    pet.graph.vp.viz_contains_taskwait[v] = 'True'
                    barrier_nodes.append(v)
                    transformation_happened = True
                    tmp_suggestion = TaskParallelismInfo(pet, v, ["taskwait"],
                                                         v_first_line,
                                                         [], [], [])
                    suggestions.append(tmp_suggestion)
#

    return suggestions


def __detect_taskloop_reduction(pet: PETGraph,
                                suggestions: [TaskParallelismInfo]):
    """detect suggested tasks which can and should be replaced by
    taskloop reduction.
    return the modified list of suggestions.
    Idea:   1. check if suggested task inside loop body
            2. check if outer loop is reduction loop
                3. if so, build reduction clause and modify suggested task
    :param pet: PET graph
    :param suggestions: List[TaskParallelismInfo]
    :return List[TaskParallelismInfo]
    """
    output = []
    # iterate over suggestions
    for s in suggestions:
        # ignore others than tasks
        if not (type(s) == Task or type(s) == TaskParallelismInfo):
            output.append(s)
            continue
        # check if s contained in reduction loop body
        red_vars_entry = __task_contained_in_reduction_loop(pet, s)
        if red_vars_entry is None:
            # s not contained in reduction loop body
            output.append(s)
        else:
            # s contained in reduction loop body
            # modify task s
            reduction_clause = "reduction("
            reduction_clause += red_vars_entry["operation"] + ":"
            reduction_clause += red_vars_entry["name"].replace(".addr", "")
            reduction_clause += ")"
            s.pragma = ["taskloop", reduction_clause]
            # append modified task to output
            output.append(s)
    return output


def __task_contained_in_reduction_loop(pet: PETGraph,
                                       task: TaskParallelismInfo):
    """detect if task is contained in loop body of a reduction loop.
    return None, if task is not contained in reduction loop.
    else, return reduction_vars entry of parent reduction loop.
    :param pet: PET graph
    :param task: TaskParallelismInfo
    :return None / {loop_line, name, reduction_line, operation}
    """
    # check if task contained in loop body
    parents = __get_parent_of_type(pet, task._node, "loop", "child", False)
    contained_in = []
    if len(parents) == 0:
        return None
    else:
        # check if task is actually contained in one of the parents
        for parent_loop, last_node in parents:
            p_start_line = pet.graph.vp.startsAtLine[parent_loop]
            p_start_line = p_start_line[p_start_line.index(":") + 1:]
            p_end_line = pet.graph.vp.endsAtLine[parent_loop]
            p_end_line = p_end_line[p_end_line.index(":") + 1:]
            t_start_line = task.start_line
            t_start_line = t_start_line[t_start_line.index(":") + 1:]
            t_end_line = task.end_line
            t_end_line = t_end_line[t_end_line.index(":") + 1:]
            if p_start_line <= t_start_line and p_end_line >= t_end_line:
                contained_in.append(parent_loop)
    # check if task is contained in a reduction loop
    for parent in contained_in:
        if pet.graph.vp.reduction[parent]:
            # get correct entry for loop from pet.reduction_vars
            for rv in pet.reduction_vars:
                if rv["loop_line"] == pet.graph.vp.startsAtLine[parent]:
                    return rv
    return None


def __set_task_contained_lines(pet: PETGraph,
                               suggestions: [TaskParallelismInfo]):
    """set region_end_line property of TaskParallelismInfo objects
    in suggestions and return the modified list.
    Regions are determined by checking if a CU contains multiple Tasks or
    Barriers and splitting up the contained source code lines accordingly.
    :param pet: PET graph
    :param suggestions: List[TaskParallelismInfo]
    :return List[TaskParallelismInfo]"""
    # group suggestions by parent CU
    output = []
    cu_to_suggestions_map = dict()
    for s in suggestions:
        # filter out non task / taskwait suggestions and append to output
        if not (type(s) == Task or type(s) == TaskParallelismInfo):
            output.append(s)
            continue
        # fill cu_to_suggestions_map
        if s.node_id in cu_to_suggestions_map:
            cu_to_suggestions_map[s.node_id].append(s)
        else:
            cu_to_suggestions_map[s.node_id] = [s]
    # order suggestions for each CU by first affected line
    for cu in cu_to_suggestions_map:
        sorted = cu_to_suggestions_map[cu]
        sorted.sort(key=lambda s: s.region_start_line)
        cu_to_suggestions_map[cu] = sorted
    # iterate over suggestions. set region_end_line to end of cu or
    # beginning of next suggestion
    for cu in cu_to_suggestions_map:
        for idx, s in enumerate(cu_to_suggestions_map[cu]):
            # check if next element exists
            if idx + 1 < len(cu_to_suggestions_map[cu]):
                # if so, set end to line prior to start of next suggestion
                end = int(cu_to_suggestions_map[cu][idx + 1].region_start_line)
                end = end - 1
                s.region_end_line = end
            else:
                # if not, set end to end of cu
                s.region_end_line = s.end_line[s.end_line.index(":") + 1:]
            # overwrite entry in cu_to_suggestions_map for s
            cu_to_suggestions_map[cu][idx] = s
    # append suggestions to output
    for cu in cu_to_suggestions_map:
        for s in cu_to_suggestions_map[cu]:
            output.append(s)
    return output


def __remove_useless_barrier_suggestions(pet: PETGraph,
                                         suggestions: [TaskParallelismInfo]):
    """remove suggested barriers which are not contained in the same
    function body with at least one suggested task.
    Returns the filtered version of the list given as a parameter.
    :param pet: PET graph
    :param suggestions: List[TaskParallelismInfo]
    :return List[TaskParallelismInfo]
    """
    # split suggestions into task and taskwait suggestions
    taskwait_suggestions = []
    task_suggestions = []
    for single_suggestion in suggestions:
        if single_suggestion.pragma[0] == "taskwait":
            taskwait_suggestions.append(single_suggestion)
        else:
            task_suggestions.append(single_suggestion)
    # get map of function body cus containing task suggestions to line number
    # of task pragmas
    relevant_function_bodies = {}
    for ts in task_suggestions:
        # get first parent cu with type function using bfs
        parent = __get_parent_of_type(pet, ts._node, "func", "child", True)
        parent = parent[0][0]  # parent like [(parent, last_node)]
        if parent not in relevant_function_bodies:
            relevant_function_bodies[parent] = [ts.pragma_line]
        else:
            relevant_function_bodies[parent].append(ts.pragma_line)
    # remove suggested barriers which are no descedants of relevant functions
    suggestions = task_suggestions
    for tws in taskwait_suggestions:
        tws_line_number = tws.pragma_line
        tws_line_number = tws_line_number[tws_line_number.index(":") + 1:]
        for rel_func_body in relevant_function_bodies.keys():
            if __check_reachability(pet, tws._node, rel_func_body, "child"):
                # remove suggested barriers where line number smaller than
                # pragma line number of task
                for line_number in relevant_function_bodies[rel_func_body]:
                    if line_number <= tws_line_number:
                        suggestions.append(tws)
                        break
    return suggestions


def __suggest_parallel_regions(pet: PETGraph,
                               suggestions: [TaskParallelismInfo]):
    """create suggestions for parallel regions based on suggested tasks.
    Parallel regions are suggested aroung each outer-most function call
    possibly leading to the creation of tasks.
    To obtain these, the child-graph is traversed in reverse,
    starting from each suggested task.
    :param pet: PET graph
    :param suggestions: List[TaskParallelismInfo]
    :return List[TaskParallelismInfo]"""
    # get task suggestions from suggestions
    task_suggestions = [s for s in suggestions if s.pragma[0] == "task"]
    # start search for each suggested task
    parents = []
    for ts in task_suggestions:
        parents += __get_parent_of_type(pet, ts._node, "func", "child", False)
    # remove duplicates
    parents = list(set(parents))
    # get outer-most parents of suggested tasks
    outer_parents = []
    # iterate over entries in parents.
    while len(parents) > 0:
        (p, last_node) = parents.pop(0)
        p_parents = __get_parent_of_type(pet, p, "func", "child", False)
        if p_parents == []:
            # p is outer
            # get last cu before p
            outer_parents.append((p, last_node))
        else:
            # append p´s parents to queue, filter out entries if already
            # present in outer_parents
            first_elements = [x[0] for x in outer_parents]
            parents += [x for x in p_parents if x[0] not in first_elements]

    # create region suggestions based on detected outer parents
    region_suggestions = []
    for parent, last_node in outer_parents:
        region_suggestions.append(ParallelRegionInfo(pet, parent,
                                  pet.graph.vp.startsAtLine[last_node],
                                  pet.graph.vp.endsAtLine[last_node]))
    return region_suggestions


def __check_reachability(pet: PETGraph, target: Vertex,
                         source: Vertex, edge_type: str):
    """check if target is reachable from source via edges of type edge_type.
    :param pet: PET graph
    :param source: Vertex
    :param target: Vertex
    :param edge_type: str
    :return Boolean"""
    visited = []
    queue = [target]
    while len(queue) > 0:
        cur_node = queue.pop(0)
        visited.append(cur_node)
        tmpList = [e for e in cur_node.in_edges()
                   if e.source() not in visited and
                   pet.graph.ep.type[e] == edge_type]
        for e in tmpList:
            if e.source() == source:
                return True
            else:
                if e.source() not in visited:
                    queue.append(e.source())
    return False


def __get_parent_of_type(pet: PETGraph, node: Vertex,
                         parent_type: str, edge_type: str, only_first: bool):
    """return parent cu nodes and the last node of the path to them as a tuple
    for the given node with type parent_type
    accessible via edges of type edge_type.
    :param pet: PET graph
    :param node: Vertex, root for the search
    :param parent_type: String, type of target node
    :param edge_type: String, type of usable edges
    :param only_first: Bool, if true, return only first parent.
        Else, return first parent for each incoming edge of node.
    :return [(Vertex, Vertex)]"""
    visited = []
    queue = [(node, None)]
    res = []
    while len(queue) > 0:
        tmp = queue.pop(0)
        (cur_node, last_node) = tmp
        last_node = cur_node
        visited.append(cur_node)
        tmpList = [e for e in cur_node.in_edges()
                   if e.source() not in visited and
                   pet.graph.ep.type[e] == edge_type]
        for e in tmpList:
            if pet.graph.vp.type[e.source()] == parent_type:
                if only_first is True:
                    return [(e.source(), last_node)]
                else:
                    res.append((e.source(), last_node))
                    visited.append(e.source())
            else:
                if e.source() not in visited:
                    queue.append((e.source(), last_node))
    return res


def __recursive_function_call_contained_in_worker_cu(pet: PETGraph,
                                                     function_call_string: str,
                                                     worker_cus: [Vertex]):
    """check if submitted function call is contained in at least one WORKER cu.
    Returns the vertex identifier of the containing cu.
    If no cu contains the function call, None is returned.
    Note: The Strings stored in recursiveFunctionCalls might contain multiple function calls at once.
          in order to apply this function correctly, make sure to split Strings in advance and supply
          one call at a time.
    :param pet: PET graph
    :param function_call_string: String representation of the recursive function call to be checked
            Ex.: fib 7:35,  (might contain ,)
    :param worker_cus: List of vertices
    """
    # remove , and whitespaces at start / end
    function_call_string = function_call_string.replace(",", "")
    while function_call_string.startswith(" "):
        function_call_string = function_call_string[1:]
    while function_call_string.endswith(" "):
        function_call_string = function_call_string[:-1]
    # function_call_string looks now like like: 'fib 7:52'

    # split String into function_name. file_id and line_number
    function_name = function_call_string[0:function_call_string.index(" ")]
    file_id = function_call_string[
              function_call_string.index(" ") + 1:
              function_call_string.index(":")]
    line_number = function_call_string[function_call_string.index(":") + 1:]

    # iterate over worker_cus
    for cur_w in worker_cus:
        cur_w_starts_at_line = pet.graph.vp.startsAtLine[cur_w]
        cur_w_ends_at_line = pet.graph.vp.endsAtLine[cur_w]
        cur_w_file_id = cur_w_starts_at_line[:cur_w_starts_at_line.index(":")]
        # check if file_id is equal
        if file_id == cur_w_file_id:
            # trim to line numbers only
            cur_w_starts_at_line = cur_w_starts_at_line[
                                   cur_w_starts_at_line.index(":") + 1:]
            cur_w_ends_at_line = cur_w_ends_at_line[
                                 cur_w_ends_at_line.index(":") + 1:]
            # check if line_number is contained
            if int(cur_w_starts_at_line) <= int(line_number) <= int(cur_w_ends_at_line):
                return cur_w
    return None


def __detect_mw_types(pet: PETGraph, main_node: Vertex):
    """The mainNode we want to compute the Task Parallelism Pattern for it
    use Breadth First Search (BFS) to detect all barriers and workers.
    1.) all child nodes become first worker if they are not marked as worker before
    2.) if a child has dependence to more than one parent node, it will be marked as barrier
    Returns list of BARRIER_WORKER pairs 2
    :param pet: PET graph
    :param main_node: root node
    """

    # first insert all the direct children of main node in a queue to use it for the BFS
    for node in find_subnodes(pet, main_node, 'child'):
        # a child node can be set to NONE or ROOT due a former detectMWNode call where it was the mainNode
        if pet.graph.vp.mwType[node] == 'NONE' or pet.graph.vp.mwType[node] == 'ROOT':
            pet.graph.vp.mwType[node] = 'FORK'

        # while using the node as the base child, we copy all the other children in a copy vector.
        # we do that because it could be possible that two children of the current node (two dependency)
        # point to two different children of another child node which results that the child node becomes BARRIER
        # instead of WORKER
        # so we copy the whole other children in another vector and when one of the children of the current node
        # does point to the other child node, we just adjust mwType and then we remove the node from the vector
        # Thus we prevent changing to BARRIER due of two dependencies pointing to two different children of
        # the other node

        # create the copy vector so that it only contains the other nodes
        other_nodes = find_subnodes(pet, main_node, 'child')
        other_nodes.remove(node)

        for other_node in other_nodes:
            if depends(pet, other_node, node):
                # print("\t" + pet.graph.vp.id[node] + "<--" + pet.graph.vp.id[other_node])
                if pet.graph.vp.mwType[other_node] == 'WORKER':
                    pet.graph.vp.mwType[other_node] = 'BARRIER'
                else:
                    pet.graph.vp.mwType[other_node] = 'WORKER'

                    # check if other_node has > 1 RAW dependencies to node
                    # -> not detected in previous step, since other_node is only
                    #    dependent of a single CU
                    raw_targets = []
                    for e in other_node.out_edges():
                        if e.target() == node:
                            if pet.graph.ep.dtype[e] == 'RAW':
                                raw_targets.append(pet.graph.vp.id[e.target()])
                    # remove entries which occur less than two times
                    raw_targets = [t for t in raw_targets if raw_targets.count(t) > 1]
                    # remove duplicates from list
                    raw_targets = list(set(raw_targets))
                    # if elements remaining, mark other_node as BARRIER
                    if len(raw_targets) > 0:
                        pet.graph.vp.mwType[other_node] = 'BARRIER'

    pairs = []
    # check for Barrier Worker pairs
    # if two barriers don't have any dependency to each other then they create a barrierWorker pair
    # so check every barrier pair that they don't have a dependency to each other -> barrierWorker
    direct_subnodes = find_subnodes(pet, main_node, 'child')
    for n1 in direct_subnodes:
        if pet.graph.vp.mwType[n1] == 'BARRIER':
            for n2 in direct_subnodes:
                if pet.graph.vp.mwType[n2] == 'BARRIER' and n1 != n2:
                    if n2 in [e.target() for e in n1.out_edges()] or n2 in [e.source() for e in n1.in_edges()]:
                        break
                    # so these two nodes are BarrierWorker, because there is no dependency between them
                    pairs.append((n1, n2))
                    pet.graph.vp.mwType[n1] = 'BARRIER_WORKER'
                    pet.graph.vp.mwType[n2] = 'BARRIER_WORKER'
    # return pairs


def __create_task_tree(pet: PETGraph, root: Vertex):
    """generates task tree data from root node

    :param pet: PET graph
    :param root: root node
    """
    root_task = Task(pet, root)
    __forks.add(root_task)
    __create_task_tree_helper(pet, root, root_task, [])


def __create_task_tree_helper(pet: PETGraph, current: Vertex, root: Task, visited_func: List[Vertex]):
    """generates task tree data recursively

    :param pet: PET graph
    :param current: current vertex to process
    :param root: root task for subtree
    :param visited_func: visited function nodes
    """
    if pet.graph.vp.type[current] == 'func':
        if current in visited_func:
            return
        else:
            visited_func.append(current)

    for child in find_subnodes(pet, current, 'child'):
        mw_type = pet.graph.vp.mwType[child]

        if mw_type in ['BARRIER', 'BARRIER_WORKER', 'WORKER']:
            task = Task(pet, child)
            root.child_tasks.append(task)
            __create_task_tree_helper(pet, child, task, visited_func)
        elif mw_type == 'FORK' and not pet.graph.vp.startsAtLine[child].endswith('16383'):
            task = Task(pet, child)
            __forks.add(task)
            __create_task_tree_helper(pet, child, task, visited_func)
        else:
            __create_task_tree_helper(pet, child, root, visited_func)