#!/usr/bin/env python3
#
#  Copyright (C) 2016 Codethink Limited
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
#        Tristan Van Berkom <tristan.vanberkom@codethink.co.uk>

from . import _site
from ._plugincontext import PluginContext
from .element import Element


# A ElementFactory creates Element instances
# in the context of a given factory
#
# Args:
#     plugin_base (PluginBase): The main PluginBase object to work with
#     searchpath (list):        Search path for external Element plugins
#
class ElementFactory(PluginContext):

    def __init__(self, plugin_base, searchpath=None):

        if searchpath is None:
            searchpath = []

        searchpath.insert(0, _site.element_plugins)
        super().__init__(plugin_base, Element, searchpath)

    # create():
    #
    # Create an Element object, the pipeline uses this to create Element
    # objects on demand for a given pipeline.
    #
    # Args:
    #    kind (str): The kind of Element to create
    #    context (object): The Context object for processing
    #    project (object): The project object
    #    meta (object): The loaded MetaElement
    #
    # Returns: A newly created Element object of the appropriate kind
    #
    # Raises:
    #    PluginError (if the kind lookup failed)
    #    LoadError (if the element itself took issue with the config)
    #
    def create(self, kind, context, project, meta):
        element_type = self.lookup(kind)
        return element_type(context, project, meta)
