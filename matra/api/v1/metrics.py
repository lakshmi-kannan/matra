# vim: tabstop=4 shiftwidth=4 softtabstop=4

#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Stack endpoint for Matra v1 ReST API.
"""

import itertools
from webob import exc

from matra.api.openstack.v1 import util
from matra.common import wsgi

from matra.openstack.common import log as logging

logger = logging.getLogger(__name__)


class MetricsController(object):
    """
    WSGI controller for stacks resource in Heat v1 API
    Implements the API actions
    """

    def __init__(self, options):
        self.options = options

    def default(self, req, **args):
        raise exc.HTTPNotFound()

    @util.tenant_local
    @util.attach_storage_engine
    def ingest_metrics(self, req):
        """
        Ingest new metrics
        """


class QuerySerializer(wsgi.JSONResponseSerializer):
    """Handles serialization of specific controller method responses."""

    def _populate_response_header(self, response, location, status):
        response.status = status
        response.headers['Location'] = location.encode('utf-8')
        response.headers['Content-Type'] = 'application/json'
        return response

    def create(self, response, result):
        self._populate_response_header(response,
                                       result['stack']['links'][0]['href'],
                                       201)
        response.body = self.to_json(result)
        return response


def create_resource(options):
    """
    Query resource factory method.
    """
    # TODO(zaneb) handle XML based on Content-type/Accepts
    deserializer = wsgi.JSONRequestDeserializer()
    serializer = QuerySerializer()
    return wsgi.Resource(QueryController(options), deserializer, serializer)
