# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2013 IBM Corp.
# All Rights Reserved.
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
Utility methods for working with WSGI servers
"""

import datetime
import errno
import json
import logging
import os
import signal
import sys
import time

import eventlet
import eventlet.greenio
from eventlet.green import socket
from eventlet.green import ssl
import eventlet.wsgi
from lxml import etree
from oslo.config import cfg
from paste import deploy
import routes
import routes.middleware
import webob.dec
import webob.exc

from matra.common import exception
from matra.openstack.common import gettextutils
from matra.openstack.common import importutils


URL_LENGTH_LIMIT = 50000

bind_opts = [
    cfg.StrOpt('bind_host', default='0.0.0.0',
               help=_('Address to bind the server.  Useful when '
                      'selecting a particular network interface.')),
    cfg.IntOpt('bind_port',
               help=_('The port on which the server will listen.'))
]

cfg.CONF.register_opts(bind_opts)

socket_opts = [
    cfg.IntOpt('backlog', default=4096,
               help=_("Number of backlog requests "
                      "to configure the socket with")),
    cfg.StrOpt('cert_file', default=None,
               help=_("Location of the SSL Certificate File "
                      "to use for SSL mode")),
    cfg.StrOpt('key_file', default=None,
               help=_("Location of the SSL Key File to use "
                      "for enabling SSL mode")),
]

cfg.CONF.register_opts(socket_opts)

workers_opts = cfg.IntOpt('workers', default=0,
                          help=_("Number of workers for matra service"))

cfg.CONF.register_opt(workers_opts)


class WritableLogger(object):
    """A thin wrapper that responds to `write` and logs."""

    def __init__(self, logger, level=logging.DEBUG):
        self.logger = logger
        self.level = level

    def write(self, msg):
        self.logger.log(self.level, msg.strip("\n"))


def get_bind_addr(conf, default_port=None):
    """Return the host and port to bind to."""
    for opt in bind_opts:
        if opt.name not in conf:
            conf.register_opt(opt)
    return (conf.bind_host, conf.bind_port or default_port)


def get_socket(conf, default_port):
    """
    Bind socket to bind ip:port in conf

    note: Mostly comes from Swift with a few small changes...

    :param conf: a cfg.ConfigOpts object
    :param default_port: port to bind to if none is specified in conf

    :returns : a socket object as returned from socket.listen or
               ssl.wrap_socket if conf specifies cert_file
    """
    bind_addr = get_bind_addr(conf, default_port)

    # TODO(jaypipes): eventlet's greened socket module does not actually
    # support IPv6 in getaddrinfo(). We need to get around this in the
    # future or monitor upstream for a fix
    address_family = [addr[0] for addr in socket.getaddrinfo(bind_addr[0],
                      bind_addr[1], socket.AF_UNSPEC, socket.SOCK_STREAM)
                      if addr[0] in (socket.AF_INET, socket.AF_INET6)][0]

    conf.register_opts(socket_opts)

    cert_file = conf.cert_file
    key_file = conf.key_file
    use_ssl = cert_file or key_file
    if use_ssl and (not cert_file or not key_file):
        raise RuntimeError(_("When running server in SSL mode, you must "
                             "specify both a cert_file and key_file "
                             "option value in your configuration file"))

    sock = None
    retry_until = time.time() + 30
    while not sock and time.time() < retry_until:
        try:
            sock = eventlet.listen(bind_addr, backlog=conf.backlog,
                                   family=address_family)
            if use_ssl:
                sock = ssl.wrap_socket(sock, certfile=cert_file,
                                       keyfile=key_file)
        except socket.error as err:
            if err.args[0] != errno.EADDRINUSE:
                raise
            eventlet.sleep(0.1)
    if not sock:
        raise RuntimeError(_("Could not bind to %(bind_addr)s"
                             "after trying for 30 seconds")
                           % {'bind_addr': bind_addr})
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # in my experience, sockets can hang around forever without keepalive
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    # This option isn't available in the OS X version of eventlet
    if hasattr(socket, 'TCP_KEEPIDLE'):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 600)

    return sock


class Server(object):
    """Server class to manage multiple WSGI sockets and applications."""

    def __init__(self, threads=1000):
        self.threads = threads
        self.children = []
        self.running = True

    def start(self, application, conf, default_port):
        """
        Run a WSGI server with the given application.

        :param application: The application to run in the WSGI server
        :param conf: a cfg.ConfigOpts object
        :param default_port: Port to bind to if none is specified in conf
        """
        def kill_children(*args):
            """Kills the entire process group."""
            self.logger.error(_('SIGTERM received'))
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            self.running = False
            os.killpg(0, signal.SIGTERM)

        def hup(*args):
            """
            Shuts down the server, but allows running requests to complete
            """
            self.logger.error(_('SIGHUP received'))
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            self.running = False

        self.application = application
        self.sock = get_socket(conf, default_port)

        self.logger = logging.getLogger('eventlet.wsgi.server')

        if conf.workers == 0:
            # Useful for profiling, test, debug etc.
            self.pool = eventlet.GreenPool(size=self.threads)
            self.pool.spawn_n(self._single_run, application, self.sock)
            return

        self.logger.info(_("Starting %d workers") % conf.workers)
        signal.signal(signal.SIGTERM, kill_children)
        signal.signal(signal.SIGHUP, hup)
        while len(self.children) < conf.workers:
            self.run_child()

    def wait_on_children(self):
        while self.running:
            try:
                pid, status = os.wait()
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    self.logger.error(_('Removing dead child %s') % pid)
                    self.children.remove(pid)
                    self.run_child()
            except OSError as err:
                if err.errno not in (errno.EINTR, errno.ECHILD):
                    raise
            except KeyboardInterrupt:
                self.logger.info(_('Caught keyboard interrupt. Exiting.'))
                break
        eventlet.greenio.shutdown_safe(self.sock)
        self.sock.close()
        self.logger.debug(_('Exited'))

    def wait(self):
        """Wait until all servers have completed running."""
        try:
            if self.children:
                self.wait_on_children()
            else:
                self.pool.waitall()
        except KeyboardInterrupt:
            pass

    def run_child(self):
        pid = os.fork()
        if pid == 0:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            self.run_server()
            self.logger.info(_('Child %d exiting normally') % os.getpid())
            return
        else:
            self.logger.info(_('Started child %s') % pid)
            self.children.append(pid)

    def run_server(self):
        """Run a WSGI server."""
        eventlet.wsgi.HttpProtocol.default_request_version = "HTTP/1.0"
        eventlet.hubs.use_hub('poll')
        eventlet.patcher.monkey_patch(all=False, socket=True)
        self.pool = eventlet.GreenPool(size=self.threads)
        try:
            eventlet.wsgi.server(self.sock,
                                 self.application,
                                 custom_pool=self.pool,
                                 url_length_limit=URL_LENGTH_LIMIT,
                                 log=WritableLogger(self.logger))
        except socket.error as err:
            if err[0] != errno.EINVAL:
                raise
        self.pool.waitall()

    def _single_run(self, application, sock):
        """Start a WSGI server in a new green thread."""
        self.logger.info(_("Starting single process server"))
        eventlet.wsgi.server(sock, application,
                             custom_pool=self.pool,
                             url_length_limit=URL_LENGTH_LIMIT,
                             log=WritableLogger(self.logger))


class Middleware(object):
    """
    Base WSGI middleware wrapper. These classes require an application to be
    initialized that will be called next.  By default the middleware will
    simply call its wrapped app, or you can override __call__ to customize its
    behavior.
    """

    def __init__(self, application):
        self.application = application

    def process_request(self, req):
        """
        Called on each request.

        If this returns None, the next application down the stack will be
        executed. If it returns a response then that response will be returned
        and execution will stop here.

        """
        return None

    def process_response(self, response):
        """Do whatever you'd like to the response."""
        return response

    @webob.dec.wsgify
    def __call__(self, req):
        response = self.process_request(req)
        if response:
            return response
        response = req.get_response(self.application)
        return self.process_response(response)


class Debug(Middleware):
    """
    Helper class that can be inserted into any WSGI application chain
    to get information about the request and response.
    """

    @webob.dec.wsgify
    def __call__(self, req):
        print(("*" * 40) + " REQUEST ENVIRON")
        for key, value in req.environ.items():
            print(key, "=", value)
        print
        resp = req.get_response(self.application)

        print(("*" * 40) + " RESPONSE HEADERS")
        for (key, value) in resp.headers.iteritems():
            print(key, "=", value)
        print

        resp.app_iter = self.print_generator(resp.app_iter)

        return resp

    @staticmethod
    def print_generator(app_iter):
        """
        Iterator that prints the contents of a wrapper string iterator
        when iterated.
        """
        print(("*" * 40) + " BODY")
        for part in app_iter:
            sys.stdout.write(part)
            sys.stdout.flush()
            yield part
        print


def debug_filter(app, conf, **local_conf):
    return Debug(app)


class Router(object):
    """
    WSGI middleware that maps incoming requests to WSGI apps.
    """

    def __init__(self, mapper):
        """
        Create a router for the given routes.Mapper.

        Each route in `mapper` must specify a 'controller', which is a
        WSGI app to call.  You'll probably want to specify an 'action' as
        well and have your controller be a wsgi.Controller, who will route
        the request to the action method.

        Examples:
          mapper = routes.Mapper()
          sc = ServerController()

          # Explicit mapping of one route to a controller+action
          mapper.connect(None, "/svrlist", controller=sc, action="list")

          # Actions are all implicitly defined
          mapper.resource("server", "servers", controller=sc)

          # Pointing to an arbitrary WSGI app.  You can specify the
          # {path_info:.*} parameter so the target app can be handed just that
          # section of the URL.
          mapper.connect(None, "/v1.0/{path_info:.*}", controller=BlogApp())
        """
        self.map = mapper
        self._router = routes.middleware.RoutesMiddleware(self._dispatch,
                                                          self.map)

    @webob.dec.wsgify
    def __call__(self, req):
        """
        Route the incoming request to a controller based on self.map.
        If no match, return a 404.
        """
        return self._router

    @staticmethod
    @webob.dec.wsgify
    def _dispatch(req):
        """
        Called by self._router after matching the incoming request to a route
        and putting the information into req.environ.  Either returns 404
        or the routed WSGI app's response.
        """
        match = req.environ['wsgiorg.routing_args'][1]
        if not match:
            return webob.exc.HTTPNotFound()
        app = match['controller']
        return app


class Request(webob.Request):
    """Add some Openstack API-specific logic to the base webob.Request."""

    def best_match_content_type(self):
        """Determine the requested response content-type."""
        supported = ('application/json',)
        bm = self.accept.best_match(supported)
        return bm or 'application/json'

    def get_content_type(self, allowed_content_types):
        """Determine content type of the request body."""
        if "Content-Type" not in self.headers:
            raise exception.InvalidContentType(content_type=None)

        content_type = self.content_type

        if content_type not in allowed_content_types:
            raise exception.InvalidContentType(content_type=content_type)
        else:
            return content_type

    def best_match_language(self):
        """Determine language for returned response."""
        all_languages = gettextutils.get_available_languages('matra')
        return self.accept_language.best_match(all_languages,
                                               default_match='en_US')


def is_json_content_type(request):
    if request.method == 'GET':
        try:
            aws_content_type = request.params.get("ContentType")
        except Exception:
            aws_content_type = None
        #respect aws_content_type when both available
        content_type = aws_content_type or request.content_type
    else:
        content_type = request.content_type
    #bug #1887882
    #for back compatible for null or plain content type
    if not content_type or content_type.startswith('text/plain'):
        content_type = 'application/json'
    if content_type in ('JSON', 'application/json')\
            and request.body.startswith('{'):
        return True
    return False


class JSONRequestDeserializer(object):
    def has_body(self, request):
        """
        Returns whether a Webob.Request object will possess an entity body.

        :param request:  Webob.Request object
        """
        if request.content_length > 0 and is_json_content_type(request):
            return True

        return False

    def from_json(self, datastring):
        try:
            return json.loads(datastring)
        except ValueError as ex:
            raise webob.exc.HTTPBadRequest(str(ex))

    def default(self, request):
        if self.has_body(request):
            return {'body': self.from_json(request.body)}
        else:
            return {}


class JSONResponseSerializer(object):

    def to_json(self, data):
        def sanitizer(obj):
            if isinstance(obj, datetime.datetime):
                return obj.isoformat()
            return obj

        response = json.dumps(data, default=sanitizer)
        logging.debug("JSON response : %s" % response)
        return response

    def default(self, response, result):
        response.content_type = 'application/json'
        response.body = self.to_json(result)


# Escape XML serialization for these keys, as the AWS API defines them as
# JSON inside XML when the response format is XML.
JSON_ONLY_KEYS = ('TemplateBody', 'Metadata')


class XMLResponseSerializer(object):

    def object_to_element(self, obj, element):
        if isinstance(obj, list):
            for item in obj:
                subelement = etree.SubElement(element, "member")
                self.object_to_element(item, subelement)
        elif isinstance(obj, dict):
            for key, value in obj.items():
                subelement = etree.SubElement(element, key)
                if key in JSON_ONLY_KEYS:
                    if value:
                        # Need to use json.dumps for the JSON inside XML
                        # otherwise quotes get mangled and json.loads breaks
                        try:
                            subelement.text = json.dumps(value)
                        except TypeError:
                            subelement.text = str(value)
                else:
                    self.object_to_element(value, subelement)
        else:
            element.text = str(obj)

    def to_xml(self, data):
        # Assumption : root node is dict with single key
        root = data.keys()[0]
        eltree = etree.Element(root)
        self.object_to_element(data.get(root), eltree)
        response = etree.tostring(eltree)
        logging.debug("XML response : %s" % response)
        return response

    def default(self, response, result):
        response.content_type = 'application/xml'
        response.body = self.to_xml(result)


class Resource(object):
    """
    WSGI app that handles (de)serialization and controller dispatch.

    Reads routing information supplied by RoutesMiddleware and calls
    the requested action method upon its deserializer, controller,
    and serializer. Those three objects may implement any of the basic
    controller action methods (create, update, show, index, delete)
    along with any that may be specified in the api router. A 'default'
    method may also be implemented to be used in place of any
    non-implemented actions. Deserializer methods must accept a request
    argument and return a dictionary. Controller methods must accept a
    request argument. Additionally, they must also accept keyword
    arguments that represent the keys returned by the Deserializer. They
    may raise a webob.exc exception or return a dict, which will be
    serialized by requested content type.
    """
    def __init__(self, controller, deserializer, serializer=None):
        """
        :param controller: object that implement methods created by routes lib
        :param deserializer: object that supports webob request deserialization
                             through controller-like actions
        :param serializer: object that supports webob response serialization
                           through controller-like actions
        """
        self.controller = controller
        self.deserializer = deserializer
        self.serializer = serializer

    @webob.dec.wsgify(RequestClass=Request)
    def __call__(self, request):
        """WSGI method that controls (de)serialization and method dispatch."""
        action_args = self.get_action_args(request.environ)
        action = action_args.pop('action', None)

        # From reading the boto code, and observation of real AWS api responses
        # it seems that the AWS api ignores the content-type in the html header
        # Instead it looks at a "ContentType" GET query parameter
        # This doesn't seem to be documented in the AWS cfn API spec, but it
        # would appear that the default response serialization is XML, as
        # described in the API docs, but passing a query parameter of
        # ContentType=JSON results in a JSON serialized response...
        content_type = request.params.get("ContentType")

        deserialized_request = self.dispatch(self.deserializer,
                                             action, request)
        action_args.update(deserialized_request)

        try:
            action_result = self.dispatch(self.controller, action,
                                          request, **action_args)
        except TypeError as err:
            logging.error(_('Exception handling resource: %s') % str(err))
            msg = _('The server could not comply with the request since\r\n'
                    'it is either malformed or otherwise incorrect.\r\n')
            err = webob.exc.HTTPBadRequest(msg)
            http_exc = translate_exception(err, request.best_match_language())
            # NOTE(luisg): We disguise HTTP exceptions, otherwise they will be
            # treated by wsgi as responses ready to be sent back and they
            # won't make it into the pipeline app that serializes errors
            raise exception.HTTPExceptionDisguise(http_exc)
        except webob.exc.HTTPException as err:
            if isinstance(err, (webob.exc.HTTPOk, webob.exc.HTTPRedirection)):
                # Some HTTPException are actually not errors, they are
                # responses ready to be sent back to the users, so we don't
                # error log, disguise or translate those
                raise
            logging.error(_("Returning %(code)s to user: %(explanation)s"),
                          {'code': err.code, 'explanation': err.explanation})
            http_exc = translate_exception(err, request.best_match_language())
            raise exception.HTTPExceptionDisguise(http_exc)
        except Exception as err:
            logging.error(_("Unexpected error occurred serving API: %s") % err)
            raise translate_exception(err, request.best_match_language())

        # Here we support either passing in a serializer or detecting it
        # based on the content type.
        try:
            serializer = self.serializer
            if serializer is None:
                if content_type == "JSON":
                    serializer = JSONResponseSerializer()
                else:
                    serializer = XMLResponseSerializer()

            response = webob.Response(request=request)
            self.dispatch(serializer, action, response, action_result)
            return response

        # return unserializable result (typically an exception)
        except Exception:
            # Here we should get API exceptions derived from MatraAPIException
            # these implement get_unserialized_body(), which allow us to get
            # a dict containing the unserialized error response.
            # We only need to serialize for JSON content_type, as the
            # exception body is pre-serialized to the default XML in the
            # MatraAPIException constructor
            # If we get something else here (e.g a webob.exc exception),
            # this will fail, and we just return it without serializing,
            # which will not conform to the expected AWS error response format
            if content_type == "JSON":
                try:
                    err_body = action_result.get_unserialized_body()
                    serializer.default(action_result, err_body)
                except Exception:
                    logging.warning("Unable to serialize exception response")

            return action_result

    def dispatch(self, obj, action, *args, **kwargs):
        """Find action-specific method on self and call it."""
        try:
            method = getattr(obj, action)
        except AttributeError:
            method = getattr(obj, 'default')
        return method(*args, **kwargs)

    def get_action_args(self, request_environment):
        """Parse dictionary created by routes library."""
        try:
            args = request_environment['wsgiorg.routing_args'][1].copy()
        except Exception:
            return {}

        try:
            del args['controller']
        except KeyError:
            pass

        try:
            del args['format']
        except KeyError:
            pass

        return args


def translate_exception(exc, locale):
    """Translates all translatable elements of the given exception."""
    exc.message = gettextutils.get_localized_message(exc.message, locale)
    if isinstance(exc, webob.exc.HTTPError):
        # If the explanation is not a Message, that means that the
        # explanation is the default, generic and not translatable explanation
        # from webop.exc. Since the explanation is the error shown when the
        # exception is converted to a response, let's actually swap it with
        # message, since message is what gets passed in at construction time
        # in the API
        if not isinstance(exc.explanation, gettextutils.Message):
            exc.explanation = exc.message
            exc.detail = ''
        else:
            exc.explanation = \
                gettextutils.get_localized_message(exc.explanation, locale)
            exc.detail = gettextutils.get_localized_message(exc.detail, locale)
    return exc


class BasePasteFactory(object):

    """A base class for paste app and filter factories.

    Sub-classes must override the KEY class attribute and provide
    a __call__ method.
    """

    KEY = None

    def __init__(self, conf):
        self.conf = conf

    def __call__(self, global_conf, **local_conf):
        raise NotImplementedError

    def _import_factory(self, local_conf):
        """Import an app/filter class.

        Lookup the KEY from the PasteDeploy local conf and import the
        class named there. This class can then be used as an app or
        filter factory.

        Note we support the <module>:<class> format.

        Note also that if you do e.g.

          key =
              value

        then ConfigParser returns a value with a leading newline, so
        we strip() the value before using it.
        """
        class_name = local_conf[self.KEY].replace(':', '.').strip()
        return importutils.import_class(class_name)


class AppFactory(BasePasteFactory):

    """A Generic paste.deploy app factory.

    This requires matra.app_factory to be set to a callable which returns a
    WSGI app when invoked. The format of the name is <module>:<callable> e.g.

      [app:apiv1app]
      paste.app_factory = matra.common.wsgi:app_factory
      matra.app_factory = matra.api.cfn.v1:API

    The WSGI app constructor must accept a ConfigOpts object and a local config
    dict as its two arguments.
    """

    KEY = 'matra.app_factory'

    def __call__(self, global_conf, **local_conf):
        """The actual paste.app_factory protocol method."""
        factory = self._import_factory(local_conf)
        return factory(self.conf, **local_conf)


class FilterFactory(AppFactory):

    """A Generic paste.deploy filter factory.

    This requires matra.filter_factory to be set to a callable which returns a
    WSGI filter when invoked. The format is <module>:<callable> e.g.

      [filter:cache]
      paste.filter_factory = matra.common.wsgi:filter_factory
      matra.filter_factory = matra.api.middleware.cache:CacheFilter

    The WSGI filter constructor must accept a WSGI app, a ConfigOpts object and
    a local config dict as its three arguments.
    """

    KEY = 'matra.filter_factory'

    def __call__(self, global_conf, **local_conf):
        """The actual paste.filter_factory protocol method."""
        factory = self._import_factory(local_conf)

        def filter(app):
            return factory(app, self.conf, **local_conf)

        return filter


def setup_paste_factories(conf):
    """Set up the generic paste app and filter factories.

    Set things up so that:

      paste.app_factory = matra.common.wsgi:app_factory

    and

      paste.filter_factory = matra.common.wsgi:filter_factory

    work correctly while loading PasteDeploy configuration.

    The app factories are constructed at runtime to allow us to pass a
    ConfigOpts object to the WSGI classes.

    :param conf: a ConfigOpts object
    """
    global app_factory, filter_factory
    app_factory = AppFactory(conf)
    filter_factory = FilterFactory(conf)


def teardown_paste_factories():
    """Reverse the effect of setup_paste_factories()."""
    global app_factory, filter_factory
    del app_factory
    del filter_factory


def paste_deploy_app(paste_config_file, app_name, conf):
    """Load a WSGI app from a PasteDeploy configuration.

    Use deploy.loadapp() to load the app from the PasteDeploy configuration,
    ensuring that the supplied ConfigOpts object is passed to the app and
    filter constructors.

    :param paste_config_file: a PasteDeploy config file
    :param app_name: the name of the app/pipeline to load from the file
    :param conf: a ConfigOpts object to supply to the app and its filters
    :returns: the WSGI app
    """
    setup_paste_factories(conf)
    try:
        return deploy.loadapp("config:%s" % paste_config_file, name=app_name)
    finally:
        teardown_paste_factories()