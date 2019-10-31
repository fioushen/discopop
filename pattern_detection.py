from queue import Queue

import numpy as np
from graph_tool.topology import shortest_path
from graph_tool.util import find_vertex
from graph_tool.all import Vertex, Graph, Edge
from typing import List, Dict, Set
from PETGraph import PETGraph




def find_subnodes(graph: Graph, node: Vertex, criteria: str) -> List[Vertex]:
    """ returns direct children of a given node
    """
    return [e.target() for e in node.out_edges() if graph.ep.type[e] == criteria]


def correlation_coefficient(v1: List[float], v2: List[float]) -> float:
    """ Calculates correlation coefficient as (A dot B) / (norm(A) * norm(B))
    """
    norm_product = np.linalg.norm(v1) * np.linalg.norm(v2)
    return 0 if norm_product == 0 else np.dot(v1, v2) / norm_product


class PatternDetector(object):
    pet: PETGraph
    loop_iterations: Dict[str, int]
    reduction_vars: List[str]
    loop_data: Dict[str, int]
    main_node: Vertex

    def __init__(self, pet_graph: PETGraph):
        self.pet = pet_graph
        self.do_all_threshold = 0.9
        self.loop_iterations = {}
        for node in pet_graph.graph.vertices():
            if pet_graph.graph.vp.name[node] == 'main':
                self.main_node = node
                break

        # parse loop iterations
        self.loop_data = {}
        with open('./data/loop_counter_output.txt') as f:
            content = f.readlines()
        for line in content:
            s = line.split(' ')
            # line = FileId + LineNr
            self.loop_data[s[0] + ':' + s[1]] = int(s[2])

        # parse reduction variables
        with open('./data/reduction.txt') as f:
            content = f.readlines()
        self.reduction_vars = []
        for line in content:
            s = line.split(' ')
            # line = FileId + LineNr
            var = {'loop_line': s[3] + ':' + s[8], 'name': s[17]}
            self.reduction_vars.append(var)

    def __print(self, lst):
        for v in lst:
            print(self.pet.graph.vp.id[v])

    def depends(self, source, target):
        if source == target:
            return False
        target_nodes = self.get_subtree_of_type(target, '*')

        for node in self.get_subtree_of_type(source, '*'):
            for dep in [e.target() for e in node.out_edges() if self.pet.graph.ep.dtype[e] == 'RAW']:
                if dep in target_nodes:
                    return True
        return False

    def is_depending(self, v_source: Vertex, v_target: Vertex, root_loop: Vertex) -> bool:
        """Detect if source vertex or one of it's children depends on target vertex or on one of it's children
        """
        children = self.get_subtree_of_type(v_target, '0')
        children.append(v_target)

        for dep in self.get_all_dependencies(v_source, root_loop):
            if dep in children:
                return True
        return False

    def is_loop_index(self, e: Edge, loops_start_lines: List[str], children: List[Vertex]) -> bool:
        """Checks, whether the variable is a loop index.
        """

        # TODO check all dependencies necessary?

        # If there is a raw dependency for var, the source cu is part of the loop
        # and the dependency occurs in loop header, then var is loop index+
        return (self.pet.graph.ep.source[e] == self.pet.graph.ep.sink[e]
                and self.pet.graph.ep.source[e] in loops_start_lines
                and e.target() in children)

    def is_readonly_inside_loop_body(self, dep: Edge, root_loop: Vertex) -> bool:
        """Checks, whether a variable is read only in loop body
        """
        loops_start_lines = [self.pet.graph.vp.startsAtLine[v]
                             for v in self.get_subtree_of_type(root_loop, '2')]

        children = self.get_subtree_of_type(root_loop, '0')

        for v in children:
            for e in v.out_edges():
                # If there is a waw dependency for var, then var is written in loop
                # (sink is always inside loop for waw/war)
                if self.pet.graph.ep.dtype[e] == 'WAR' or self.pet.graph.ep.dtype[e] == 'WAW':
                    if (self.pet.graph.ep.var[dep] == self.pet.graph.ep.var[e]
                            and not (self.pet.graph.ep.sink[e] in loops_start_lines)):
                        return False
            for e in v.in_edges():
                # If there is a reverse raw dependency for var, then var is written in loop
                # (source is always inside loop for reverse raw)
                if self.pet.graph.ep.dtype[e] == 'RAW':
                    if (self.pet.graph.ep.var[dep] == self.pet.graph.ep.var[e]
                            and not (self.pet.graph.ep.source[e] in loops_start_lines)):
                        return False
        return True

    def get_all_dependencies(self, node: Vertex, root_loop: Vertex) -> Set[Vertex]:
        """Returns all data dependencies of the node and it's children
        """
        dep_set = set()
        children = self.get_subtree_of_type(node, '0')

        loops_start_lines = [self.pet.graph.vp.startsAtLine[v]
                             for v in self.get_subtree_of_type(root_loop, '2')]

        for v in children:
            for e in v.out_edges():
                if self.pet.graph.ep.type[e] == 'dependence' and self.pet.graph.ep.dtype[e] == 'RAW':
                    if not (self.is_loop_index(e, loops_start_lines, self.get_subtree_of_type(root_loop, '0'))
                            and self.is_readonly_inside_loop_body(e, root_loop)):
                        dep_set.add(e.target())
        return dep_set

    def get_subtree_of_type(self, root: Vertex, type: str) -> List[Vertex]:
        """Returns all nodes of a given type from a subtree
        """
        res = []
        if self.pet.graph.vp.type[root] == type or type == '*':
            res.append(root)

        for e in root.out_edges():
            if self.pet.graph.ep.type[e] == 'child':
                res.extend(self.get_subtree_of_type(e.target(), type))
        return res

    def is_pipeline_subnode(self, root: Vertex, current: Vertex, children_start_lines: List[str]) -> bool:
        """Checks if node is a valid subnode for pipeline
        """
        r_start = self.pet.graph.vp.startsAtLine[root]
        r_end = self.pet.graph.vp.endsAtLine[root]
        c_start = self.pet.graph.vp.startsAtLine[current]
        c_end = self.pet.graph.vp.endsAtLine[current]
        return not (c_start == r_start and c_end == r_start
                    or c_start == r_end and c_end == r_end
                    or c_start == c_end and c_start in children_start_lines)

    def __detect_pipeline_loop(self):
        """Search pipeline pattern on all the loops within the application
        """
        for node in find_vertex(self.pet.graph, self.pet.graph.vp.type, '2'):
            self.pet.graph.vp.pipeline[node] = self.__detect_pipeline(node)
            if self.pet.graph.vp.pipeline[node] > 0:
                print('Pipeline at', self.pet.graph.vp.id[node])
                print('start:', self.pet.graph.vp.startsAtLine[node])
                print('end:', self.pet.graph.vp.endsAtLine[node])

    def __detect_pipeline(self, root: Vertex) -> float:
        """Calculate pipeline value for node. Returns pipeline scalar value
        """

        # TODO how deep
        children_start_lines = [self.pet.graph.vp.startsAtLine[v]
                                for v in find_subnodes(self.pet.graph, root, 'child')
                                if self.pet.graph.vp.type[v] == '2']

        loop_subnodes = [v for v in find_subnodes(self.pet.graph, root, 'child')
                         if self.is_pipeline_subnode(root, v, children_start_lines)]

        # No chain of stages found
        if len(loop_subnodes) < 2:
            self.pet.graph.vp.pipeline[root] = -1
            return 0

        graph_vector = []
        for i in range(0, len(loop_subnodes) - 1):
            graph_vector.append(1 if self.is_depending(loop_subnodes[i + 1], loop_subnodes[i], root) else 0)

        pipeline_vector = []
        for i in range(0, len(loop_subnodes) - 1):
            pipeline_vector.append(1)

        min_weight = 1
        for i in range(0, len(loop_subnodes) - 1):
            for j in range(i + 1, len(loop_subnodes)):
                if self.is_depending(loop_subnodes[i], loop_subnodes[j], root):
                    # TODO whose corresponding entry in the graph matrix is nonzero?
                    node_weight = 1 - (j - i) / (len(loop_subnodes) - 1)
                    if min_weight > node_weight > 0:
                        min_weight = node_weight

        if min_weight == 1:
            graph_vector.append(0)
            pipeline_vector.append(0)
        else:
            graph_vector.append(1)
            pipeline_vector.append(min_weight)
        return correlation_coefficient(graph_vector, pipeline_vector)

    def __detect_do_all_loop(self):
        """Search for do-all loop pattern
        """
        for node in find_vertex(self.pet.graph, self.pet.graph.vp.type, '2'):
            val = self.__detect_do_all(node)
            if val > self.do_all_threshold:
                self.pet.graph.vp.doAll[node] = val
                if not self.pet.graph.vp.reduction[node]:
                    print('Do All at', self.pet.graph.vp.id[node])
                    print('Coefficient', val)

    def __detect_do_all(self, root: Vertex):
        """Calculate do-all value for node. Returns dü-all scalar value
        """
        graph_vector = []

        subnodes = find_subnodes(self.pet.graph, root, 'child')

        for i in range(0, len(subnodes)):
            for j in range(i, len(subnodes)):
                if self.is_depending(subnodes[i], subnodes[j], root):
                    graph_vector.append(0)
                else:
                    graph_vector.append(1)

        pattern_vector = [1 for _ in graph_vector]

        if np.linalg.norm(graph_vector) == 0:
            return 0

        return correlation_coefficient(graph_vector, pattern_vector)

    def __detect_task_parallelism_loop(self):
        """computes the Task Parallelism Pattern for a node:
        (Automatic Parallel Pattern Detection in the Algorithm Structure Design Space p.46)
        1.) first merge all children of the node -> all children nodes get the dependencies
            of their children nodes and the list of the children nodes (saved in node.childrenNodes)
        2.) To detect Task Parallelism, we use Breadth First Search (BFS)
            a.) the hotspot becomes a fork
            b.) all child nodes become first worker if they are not marked as worker before
            c.) if a child has dependence to more than one parent node, it will be marked as barrier
        3.) if two barriers can run in parallel they are marked as barrierWorkers.
            Two barriers can run in parallel if there is not a directed path from one to the other
        """
        for node in self.pet.graph.vertices():
            if self.pet.graph.vp.type[node] == '3':
                continue
            if find_subnodes(self.pet.graph, node, 'child'):
                #print(self.pet.graph.vp.id[node])
                self.detect_task_parallelism(node)

            if self.pet.graph.vp.mwType[node] == 'NONE':
                self.pet.graph.vp.mwType[node] = 'ROOT'

        for node in self.pet.graph.vertices():
            if self.pet.graph.vp.type[node] != '3':
                print(self.pet.graph.vp.id[node] + ' ' + self.pet.graph.vp.mwType[node])# + ' ' + self.pet.graph.vp.type[node])

    def detect_task_parallelism(self, main_node: Vertex):
        """The mainNode we want to compute the Task Parallelism Pattern for it
        use Breadth First Search (BFS) to detect all barriers and workers.
        1.) all child nodes become first worker if they are not marked as worker before
        2.) if a child has dependence to more than one parent node, it will be marked as barrier
        Returns list of BARRIER_WORKER pairs 2
        """

        id = self.pet.graph.vp.id[main_node]
        # print("working:", id)
        # first insert all the direct children of mainnode in a queue to use it for the BFS
        for node in find_subnodes(self.pet.graph, main_node, 'child'):
            # a child node can be set to NONE or ROOT due a former detectMWNode call where it was the mainNode
            if self.pet.graph.vp.mwType[node] == 'NONE' or self.pet.graph.vp.mwType[node] == 'ROOT':
                self.pet.graph.vp.mwType[node] = 'FORK'

            # while using the node as the base child, we copy all the other children in a copy vector.
            # we do that because it could be possible that two children of the current node (two dependency)
            # point to two different children of another child node which results that the childnode becomes BARRIER
            # instead of WORKER
            # so we copy the whole other children in another vector and when one of the children of the current node
            # does point to the other child node, we just adjust mwType and then we remove the node from the vector
            # Thus we prevent changing to BARRIER due of two dependencies pointing to two different children of
            # the other node

            # create the copy vector so that it only contains the other nodes
            other_nodes = find_subnodes(self.pet.graph, main_node, 'child')
            other_nodes.remove(node)

            for other_node in other_nodes:
                if self.depends(other_node, node):
                    # print("\t" + self.pet.graph.vp.id[node] + "<--" + self.pet.graph.vp.id[other_node])
                    if self.pet.graph.vp.mwType[other_node] == 'WORKER':
                        self.pet.graph.vp.mwType[other_node] = 'BARRIER'
                    else:
                        self.pet.graph.vp.mwType[other_node] = 'WORKER'

        pairs = []
        # check for Barrier Worker pairs
        # if two barriers don't have any dependency to each other then they create a barrierWorker pair
        # so check every barrier pair that they don't have a dependency to each other -> barrierWorker
        direct_subnodes = find_subnodes(self.pet.graph, main_node, 'child')
        for n1 in direct_subnodes:
            if self.pet.graph.vp.mwType[n1] == 'BARRIER':
                for n2 in direct_subnodes:
                    # TODO n1 -> n2
                    if self.pet.graph.vp.mwType[n2] == 'BARRIER' and n1 != n2:
                        if n2 in [e.target() for e in n1.out_edges()] or n2 in [e.source() for e in n1.in_edges()]:
                            break
                        # so these two nodes are BarrierWorker, because there is no dependency between them
                        pairs.append((n1, n2))
                        self.pet.graph.vp.mwType[n1] = 'BARRIER_WORKER'
                        self.pet.graph.vp.mwType[n2] = 'BARRIER_WORKER'

        return pairs

    '''
    * function					: merges all children and
    dependencies of the children of all nodes
    * @param loopType			: if set to true -> then just look for
    type = loop
    * @param removeDummies		: don't regard the dummy nodes (type = 3)
    Main Level:					   node1
    .......... node2 ....
                                            /			|
    \ Level I:		child1		  child2		child3
                            /    |    \		  / | \			/ | \
    Level II: child11
    ...
    .
    .
    
    * 1.) get node from nodeMap
    *	I.) iterate through all children in Level I
    *	II.) get the whole children nodes (Level II+) of the child in Level I
    and save them in a vector under property node.wholeSubNodes *	III.) iterate
    through all children nodes (Level II+) of the Level I child and adjust the
    dependencies:
    *		a.) the dependencies remain if they that are pointing to any
    other node of the child node (Level I+) of the main node in the Main Level *
    b.) dependencies pointing to any node out of the tree of the node in the Main
    Level are removed
    * 2.) do Step I for all nodes in nodeMap
    '''

    def __merge(self, loop_type: bool, remove_dummies: bool):
        """
        Removes dummy nodes
        :param loop_type: loops only
        :param remove_dummies: remove dummy nodes
        :return:
        """
        # iterate through all entries of the map -> Nodes
        # set the ids of all children
        for node in self.pet.graph.vertices():
            if not loop_type or self.pet.graph.vp.type[node] == '2':
                # if the main node is dummy and we should remove dummies, then do not
                # insert it in nodeMapComputed
                if remove_dummies and self.pet.graph.vp.type[node] == '3':
                    continue

                sub_nodes = []
                for e in node.out_edges():
                    if self.pet.graph.ep.type[e] == 'child':
                        if remove_dummies and self.pet.graph.vp.type[e.target()] == '3':
                            self.pet.graph.remove_edge(e)
                        else:
                            sub_nodes.append(e.target())

            # TODO optimization opportunity: copy all dependency edges to the root node

    def __is_reduction_var(self, line: str, name: str) -> bool:
        """
        determines, whether or not the given variable is reduction variable
        :param line: loop line number
        :param name: variable name
        :return: true if is reduction variable
        """
        return any(rv for rv in self.reduction_vars if rv['loop_line'] == line and rv['name'] == name)

    def __detect_reduction_loop(self):
        """Search for reduction pattern
        """
        for node in find_vertex(self.pet.graph, self.pet.graph.vp.type, '2'):
            if self.__detect_reduction(node):
                self.pet.graph.vp.reduction[node] = True
                print('Reduction at', self.pet.graph.vp.id[node])

    def __detect_reduction(self, root: Vertex) -> bool:
        """
        Detects reduction pattern in loop
        :param root: the loop node
        :return: true if is reduction loop
        """
        if self.pet.graph.vp.type[root] != '2':
            return False

        vars = set()
        for node in self.get_subtree_of_type(root, '0'):
            for v in self.pet.graph.vp.localVars[node]:
                vars.add(v)
            for v in self.pet.graph.vp.globalVars[node]:
                vars.add(v)

        reduction_vars = [v for v in vars if self.__is_reduction_var(self.pet.graph.vp.startsAtLine[root], v)]
        return bool(reduction_vars)

    def __detect_geometric_decomposition_loop(self):
        """
        Detects geometric decomposition
        :return:
        """
        for node in find_vertex(self.pet.graph, self.pet.graph.vp.type, '1'):
            id = self.pet.graph.vp.id[node]
            val = self.__detect_geometric_decomposition(node)
            if val:
                self.pet.graph.vp.geomDecomp[node] = val
                id = self.pet.graph.vp.id[node]
                if self.__test_chunk_limit(node):
                    print('Geometric decomposition at', self.pet.graph.vp.id[node])

    def __test_chunk_limit(self, node: Vertex) -> bool:
        """
        Tests, whether or not the node satisfies chunk limit
        :param node: the node
        :return:
        """
        min_iterations_count = 9999999999999
        inner_loop_iter = {}

        children = [e.target() for e in node.out_edges() if self.pet.graph.ep.type[e] == 'child'
                    and self.pet.graph.vp.type[e.target()] == '2']
        for func_child in [e.target() for e in node.out_edges() if self.pet.graph.ep.type[e] == 'child'
                                                                   and self.pet.graph.vp.type[e.target()] == '1']:
            children.extend([e.target() for e in func_child.out_edges() if self.pet.graph.ep.type[e] == 'child'
                    and self.pet.graph.vp.type[e.target()] == '2'])

        for child in children:
            id = self.pet.graph.vp.id[child]
            inner_loop_iter[self.pet.graph.vp.startsAtLine[child]] = self.__iterations_count(child)

        for k, v in inner_loop_iter.items():
            min_iterations_count = min(min_iterations_count, v)

        return inner_loop_iter and min_iterations_count > 0

    def __iterations_count(self, node: Vertex) -> int:
        """
        Counts the iterations in the specified node
        :param node: the loop node
        :return: number of iterations
        """
        id = self.pet.graph.vp.id[node]
        if not (node in self.loop_iterations):
            loop_iter = self.__get_loop_iterations(self.pet.graph.vp.startsAtLine[node])
            parent_iter = self.__get_parent_iterations(node)

            if loop_iter < parent_iter:
                self.loop_iterations[node] = loop_iter
            elif loop_iter == 0 or parent_iter == 0:
                self.loop_iterations[node] = 0
            else:
                self.loop_iterations[node] = loop_iter // parent_iter

        return self.loop_iterations[node]

    def __get_loop_iterations(self, line: str) -> int:
        """
        Calculates the number of iterations in specified loop
        :param line: start line of the loop
        :return: number of iterations
        """
        return self.loop_data.get(line, 0)

    def __get_parent_iterations(self, node: Vertex) -> int:
        """
         Calculates the number of iterations in parent of loop
        :param node:
        :return:
        """
        parent = [e.source() for e in node.in_edges() if self.pet.graph.ep.type[e] == 'child']

        max_iter = 1
        while parent:
            node = parent[0]
            if self.pet.graph.vp.type[node] == '2':
                max_iter = max(1, self.__get_loop_iterations(self.pet.graph.vp.startsAtLine[node]))
                break
            parent = [e.source() for e in node.in_edges() if self.pet.graph.ep.type[e] == 'child']

        return max_iter

    def __detect_geometric_decomposition(self, root: Vertex) -> bool:
        """
        Detects geometric decomposition pattern
        :param root: root node
        :return: true if GD pattern was discovered
        """
        for child in self.get_subtree_of_type(root, '2'):
            if (self.pet.graph.vp.doAll[child] < self.do_all_threshold
                    and not self.pet.graph.vp.reduction[child]):
                return False

        for child in find_subnodes(self.pet.graph, root, '1'):
            for child2 in find_subnodes(self.pet.graph, child, '2'):
                if (self.pet.graph.vp.doAll[child2] < self.do_all_threshold
                        and not self.pet.graph.vp.reduction[child2]):
                    return False

        return True

    def detect_patterns(self):
        """
        Runs pattern discovery on the CU graph
        :return:
        """
        self.__merge(False, True)

        self.__detect_pipeline_loop()

        # reduction before doall!
        self.__detect_reduction_loop()
        self.__detect_do_all_loop()

        self.__detect_task_parallelism_loop()
        self.__detect_geometric_decomposition_loop()
