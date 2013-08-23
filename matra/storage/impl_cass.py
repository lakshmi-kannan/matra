# -*- encoding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""
Cass storage backend
"""
import pycassa

from matra.openstack.common import log
from matra.openstack.common import network_utils
from matra.storage import base

LOG = log.getLogger(__name__)


class CassStorage(base.StorageEngine):
    '''
    Put data into cassandra
    '''

    @staticmethod
    def get_connection(conf):
        '''
        Return a connection instance based on configuration
        '''
        return Connection(conf)


class Connection(base.Connection):
    '''
    Cassandra connection
    '''

    # TODO (lakshmi): Fetch these from configs
    CASS_KEYSPACE='DATA'
    METRICS_FULL_CF='metrics_5m'

    def __init__(self, conf):
        # TODO (lakshmi): Support in memory connections for testing
        opts = self._parse_connection_url(conf.database.connection)
        self.conn_pool = self._get_connection_pool(opts)


    def _get_connection_pool(conf):
        return pycassa.ConnectionPool(CASS_KEYSPACE, server_list=':'.join([opts.host, str(opts.port)]))


    @staticmethod
    def _get_connection(conf):
        '''
        Return a connection to the database
        '''
        return self.conn_pool.get()

    @staticmethod
    def _parse_connection_url(url):
        '''
        Parse connection parameters from a database url.
        '''
        opts = {}
        result = network_utils.urlsplit(url)
        if ':' in result.netloc:
            opts['host'], port = result.netloc.split(':')
        else:
            opts['host'] = result.netloc
            port = 9160
        opts['port'] = port and int(port) or 9160
        return opts


    def ingest_metrics(self, data):
        pass
