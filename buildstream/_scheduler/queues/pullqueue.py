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
#        Jürg Billeter <juerg.billeter@codethink.co.uk>

# Local imports
from . import Queue, QueueStatus
from ..resources import ResourceType
from ..jobs import JobStatus
from ..._exceptions import SkipJob
from ..._message import MessageType


# A queue which pulls element artifacts
#
class PullQueue(Queue):

    action_name = "Pull"
    complete_name = "Pulled"
    resources = [ResourceType.DOWNLOAD, ResourceType.CACHE]

    def process(self, element):
        # returns whether an artifact was downloaded or not
        if not element._pull():
            raise SkipJob(self.action_name)

    def status(self, element):
        if not element._is_required():
            # Artifact is not currently required but it may be requested later.
            # Keep it in the queue.
            self._message(element, MessageType.INFO,
                          "{} queue holding element, it is not required".format(self.action_name))

            return QueueStatus.WAIT

        if not element._can_query_cache():
            self._message(element, MessageType.INFO,
                          "{} queue holding element, cannot query cache".format(self.action_name),
                          detail="Assemble scheduled: {}, Strict cache key: {}"
                          .format(element._Element__assemble_scheduled,
                                  element._Element__strict_cache_key))
            return QueueStatus.WAIT

        if element._pull_pending():
            self._message(element, MessageType.INFO,
                          "{} queue element ready, pull is pending".format(self.action_name))
            return QueueStatus.READY
        else:
            self._message(element, MessageType.INFO,
                          "{} queue skipping element, pull is not pending".format(self.action_name))
            return QueueStatus.SKIP

    def done(self, _, element, result, status):

        if status == JobStatus.FAIL:
            return False

        element._pull_done()

        # Build jobs will check the "approximate" size first. Since we
        # do not get an artifact size from pull jobs, we have to
        # actually check the cache size.
        if status == JobStatus.OK:
            self._scheduler.check_cache_size()
