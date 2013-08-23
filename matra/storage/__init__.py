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
Storage backend management
"""


import urlparse

from oslo.config import cfg
from stevedore import driver

from matra.openstack.common import log
from matra import utils
from matra import service


LOG = log.getLogger(__name__)

STORAGE_ENGINE_NAMESPACE = 'matra.storage'

OLD_STORAGE_OPTS = [
    cfg.StrOpt('database_connection',
               secret=True,
               default=None,
               help='DEPRECATED - Database connection string',
               ),
]

cfg.CONF.register_opts(OLD_STORAGE_OPTS)


STORAGE_OPTS = [
    cfg.IntOpt('time_to_live',
               default=-1,
               help="""number of seconds that samples are kept
in the database for (<= 0 means forever)"""),
]

cfg.CONF.register_opts(STORAGE_OPTS, group='database')

cfg.CONF.import_opt('connection',
                    'matra.openstack.common.db.sqlalchemy.session',
                    group='database')


def get_engine(conf):
    """Load the configured engine and return an instance."""
    if conf.database_connection:
        conf.set_override('connection', conf.database_connection,
                          group='database')
    engine_name = urlparse.urlparse(conf.database.connection).scheme
    LOG.debug('looking for %r driver in %r',
              engine_name, STORAGE_ENGINE_NAMESPACE)
    mgr = driver.DriverManager(STORAGE_ENGINE_NAMESPACE,
                               engine_name,
                               invoke_on_load=True)
    return mgr.driver


#TODO (lakshmi): I really think these don't belong here.
class StorageBadVersion(Exception):
    """Error raised when the storage backend version is not good enough."""


def get_connection(conf):
    """Return an open connection to the database."""
    return get_engine(conf).get_connection(conf)


def dbsync():
    service.prepare_service()
    get_connection(cfg.CONF).upgrade()


def expirer():
    service.prepare_service()
    LOG.debug("Clearing expired metering data")
    storage_conn = get_connection(cfg.CONF)
    storage_conn.clear_expired_metering_data(
        cfg.CONF.database.time_to_live)
