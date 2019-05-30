#
#  Copyright (C) 2019 Bloomberg L.P.
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.	 See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library. If not, see <http://www.gnu.org/licenses/>.
#
#  Authors:
#        Benjamin Schubert <bschubert@bloomberg.net>

# Documentation for each class and method here can be found in the adjacent
# implementation file (_yaml.pyx)

cdef class Node:

    cdef public object value
    cdef public int file_index
    cdef public int line
    cdef public int column


cdef class ProvenanceInformation:

    cdef public Node node
    cdef str displayname
    cdef public str filename, shortname
    cdef public int col, line
    cdef public object project, toplevel
    cdef public bint is_synthetic


cpdef object node_get(Node node, object expected_type, str key, list indices=*, object default_value=*, bint allow_none=*)
cpdef void node_set(Node node, object key, object value, list indices=*) except *
cpdef list node_keys(object node)
cpdef ProvenanceInformation node_get_provenance(Node node, str key=*, list indices=*)