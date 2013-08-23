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

from functools import wraps
from matra import storage

def tenant_local(handler):
    '''
    Decorator for a handler method that sets the correct tenant_id in the
    request context.
    '''
    @wraps(handler)
    def handle_method(controller, req, tenant_id, **kwargs):
        req.context.tenant_id = tenant_id
        return handler(controller, req, **kwargs)

    return handle_method

def attach_storage_engine(handler):
    '''
    Decorator for handler method that sets the storage connection
    to execute the request
    '''
    @wraps(handler)
    def attach_engine(controller, req, tenant_id, **kwargs):
        req.context.storage_engine = storage.get_engine(conf)
        return handler(controller, req, **kwargs)

    return attach_engine
