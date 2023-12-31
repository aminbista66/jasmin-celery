import pickle
import glob
import os
import string
import urllib.request, urllib.parse, urllib.error
import random

from unittest.mock import Mock, call
from twisted.cred import portal
from twisted.cred.checkers import AllowAnonymousAccess, InMemoryUsernamePasswordDatabaseDontUse
from twisted.internet import defer, reactor
from twisted.internet.protocol import Factory
from twisted.spread import pb
from twisted.trial.unittest import TestCase
from twisted.web import server
from twisted.web.client import Agent
from treq import text_content
from treq.client import HTTPClient

import jasmin
from jasmin.managers.clients import SMPPClientManagerPB
from jasmin.managers.configs import SMPPClientPBConfig, DLRLookupConfig
from jasmin.managers.dlr import DLRLookup
from jasmin.managers.proxies import SMPPClientManagerPBProxy
from jasmin.protocols.http.configs import HTTPApiConfig
from jasmin.protocols.http.server import HTTPApi
from jasmin.protocols.smpp.configs import SMPPClientConfig
from tests.protocols.smpp.smsc_simulator import *
from jasmin.queues.configs import AmqpConfig
from jasmin.queues.factory import AmqpFactory
from jasmin.redis.client import ConnectionWithConfiguration
from jasmin.redis.configs import RedisForJasminConfig
from jasmin.routing.Filters import GroupFilter
from jasmin.routing.Interceptors import DefaultInterceptor, StaticMTInterceptor
from jasmin.routing.Routes import DefaultRoute, StaticMTRoute
from jasmin.routing.configs import DLRThrowerConfig
from jasmin.routing.configs import RouterPBConfig
from jasmin.routing.jasminApi import *
from jasmin.routing.proxies import RouterPBProxy
from jasmin.routing.router import RouterPB
from tests.routing.http_server import AckServer
from jasmin.routing.throwers import DLRThrower
from jasmin.tools.cred.portal import JasminPBRealm
from jasmin.tools.proxies import ConnectError
from jasmin.tools.spread.pb import JasminPBPortalRoot
from smpp.pdu.pdu_types import (AddrTon, AddrNpi)


@defer.inlineCallbacks
def waitFor(seconds):
    # Wait seconds
    waitDeferred = defer.Deferred()
    reactor.callLater(seconds, waitDeferred.callback, None)
    yield waitDeferred


def composeMessage(characters, length):
    # Convert set to sequence
    # c.f. https://docs.python.org/3/library/random.html#random.sample
    characters = list(characters)

    if length <= len(characters):
        return ''.join(random.sample(characters, length))
    else:
        s = ''
        while len(s) < length:
            s += ''.join(random.sample(characters, len(characters)))
        return s[:length]


def id_generator(size=12, chars=string.ascii_uppercase + string.digits):
    return ''.join(random.choice(chars) for x in range(size))


class RouterPBTestCase(TestCase):
    def setUp(self, authentication=False):
        # Initiating config objects without any filename
        # will lead to setting defaults and that's what we
        # need to run the tests
        self.RouterPBConfigInstance = RouterPBConfig()

        # Launch the router server
        self.pbRoot_f = RouterPB(self.RouterPBConfigInstance)

        # Mock callbacks
        # will be used for assertions
        self.pbRoot_f.bill_request_submit_sm_resp_callback = Mock(
            wraps=self.pbRoot_f.bill_request_submit_sm_resp_callback)
        self.pbRoot_f.deliver_sm_callback = Mock(wraps=self.pbRoot_f.deliver_sm_callback)

        p = portal.Portal(JasminPBRealm(self.pbRoot_f))
        if not authentication:
            p.registerChecker(AllowAnonymousAccess())
        else:
            c = InMemoryUsernamePasswordDatabaseDontUse()
            c.addUser('test_user', md5('test_password'.encode('ascii')).digest())
            p.registerChecker(c)
        jPBPortalRoot = JasminPBPortalRoot(p)
        self.PBServer = reactor.listenTCP(0, pb.PBServerFactory(jPBPortalRoot))
        self.pbPort = self.PBServer.getHost().port

    @defer.inlineCallbacks
    def tearDown(self):
        yield self.disconnect()

        yield self.PBServer.stopListening()
        yield self.pbRoot_f.cancelPersistenceTimer()


class HttpServerTestCase(RouterPBTestCase):
    def setUp(self, interceptorpb_client=None):
        RouterPBTestCase.setUp(self)

        # Initiating config objects without any filename
        # will lead to setting defaults and that's what we
        # need to run the tests
        httpApiConfigInstance = HTTPApiConfig()
        SMPPClientPBConfigInstance = SMPPClientPBConfig()
        SMPPClientPBConfigInstance.authentication = False

        # Smpp client manager is required for HTTPApi instanciation
        self.clientManager_f = SMPPClientManagerPB(SMPPClientPBConfigInstance)

        # Add RouterPB to Smpp client manager
        self.clientManager_f.addRouterPB(self.pbRoot_f)

        # Launch the http server
        httpApi = HTTPApi(self.pbRoot_f, self.clientManager_f, httpApiConfigInstance, interceptorpb_client)
        self.httpServer = reactor.listenTCP(httpApiConfigInstance.port, server.Site(httpApi))
        self.httpPort = httpApiConfigInstance.port

    @defer.inlineCallbacks
    def tearDown(self):
        yield RouterPBTestCase.tearDown(self)

        yield self.httpServer.stopListening()


class SMPPClientManagerPBTestCase(HttpServerTestCase):
    @defer.inlineCallbacks
    def setUp(self, interceptorpb_client=None):
        HttpServerTestCase.setUp(self, interceptorpb_client)

        # Initiating config objects without any filename
        # will lead to setting defaults and that's what we
        # need to run the tests
        AMQPServiceConfigInstance = AmqpConfig()
        AMQPServiceConfigInstance.reconnectOnConnectionLoss = False

        # Launch AMQP Broker
        self.amqpBroker = AmqpFactory(AMQPServiceConfigInstance)
        self.amqpBroker.preConnect()
        self.amqpClient = reactor.connectTCP(AMQPServiceConfigInstance.host, AMQPServiceConfigInstance.port,
                                             self.amqpBroker)

        # Wait for AMQP Broker connection to get ready
        yield self.amqpBroker.getChannelReadyDeferred()

        # Add the broker to the RouterPB
        yield self.pbRoot_f.addAmqpBroker(self.amqpBroker)

        # Setup smpp client manager pb
        yield self.clientManager_f.addAmqpBroker(self.amqpBroker)
        p = portal.Portal(JasminPBRealm(self.clientManager_f))
        p.registerChecker(AllowAnonymousAccess())
        jPBPortalRoot = JasminPBPortalRoot(p)
        self.CManagerServer = reactor.listenTCP(0, pb.PBServerFactory(jPBPortalRoot))
        self.CManagerPort = self.CManagerServer.getHost().port

        # Start DLRThrower
        DLRThrowerConfigInstance = DLRThrowerConfig()
        self.DLRThrower = DLRThrower(DLRThrowerConfigInstance)
        yield self.DLRThrower.addAmqpBroker(self.amqpBroker)

        # Connect to redis server
        RedisForJasminConfigInstance = RedisForJasminConfig()
        self.redisClient = yield ConnectionWithConfiguration(RedisForJasminConfigInstance)
        # Authenticate and select db
        if RedisForJasminConfigInstance.password is not None:
            yield self.redisClient.auth(RedisForJasminConfigInstance.password)
            yield self.redisClient.select(RedisForJasminConfigInstance.dbid)
        # Connect CM with RC:
        self.clientManager_f.addRedisClient(self.redisClient)

        # Set a smpp client manager proxy instance
        self.SMPPClientManagerPBProxy = SMPPClientManagerPBProxy()

        # Should we start local dlr lookup ?
        self.dlrlookup_config = DLRLookupConfig()
        self.dlrlookup = DLRLookup(self.dlrlookup_config, self.amqpBroker, self.redisClient)
        yield self.dlrlookup.subscribe()

    @defer.inlineCallbacks
    def tearDown(self):
        yield HttpServerTestCase.tearDown(self)

        if self.SMPPClientManagerPBProxy.isConnected:
            yield self.SMPPClientManagerPBProxy.disconnect()
        yield self.CManagerServer.stopListening()
        for q in self.amqpBroker.queues:
            yield self.amqpBroker.chan.queue_delete(queue=q)
        yield self.amqpClient.disconnect()
        yield self.redisClient.disconnect()


class AuthenticatedTestCases(RouterPBProxy, RouterPBTestCase):
    @defer.inlineCallbacks
    def setUp(self, authentication=False):
        yield RouterPBTestCase.setUp(self, authentication=True)

    @defer.inlineCallbacks
    def test_connect_success(self):
        yield self.connect('127.0.0.1', self.pbPort, 'test_user', 'test_password')

    @defer.inlineCallbacks
    def test_connect_failure(self):
        try:
            yield self.connect('127.0.0.1', self.pbPort, 'test_anyuser', 'test_wrongpassword')
        except ConnectError as e:
            self.assertEqual(str(e), 'Authentication error test_anyuser')
        except Exception as e:
            self.assertTrue(False, "ConnectError not raised, got instead a %s" % type(e))
        else:
            self.assertTrue(False, "ConnectError not raised")

        self.assertFalse(self.isConnected)

    @defer.inlineCallbacks
    def test_connect_non_anonymous(self):
        try:
            yield self.connect('127.0.0.1', self.pbPort)
        except ConnectError as e:
            self.assertEqual(str(e), 'Anonymous connection is not authorized !')
        except Exception as e:
            self.assertTrue(False, "ConnectError not raised, got instead a %s" % type(e))
        else:
            self.assertTrue(False, "ConnectError not raised")

        self.assertFalse(self.isConnected)


class BasicTestCases(RouterPBProxy, RouterPBTestCase):
    @defer.inlineCallbacks
    def test_version_release(self):
        yield self.connect('127.0.0.1', self.pbPort)

        version_release = yield self.version_release()

        self.assertEqual(version_release, jasmin.get_release())

    @defer.inlineCallbacks
    def test_version(self):
        yield self.connect('127.0.0.1', self.pbPort)

        version = yield self.version()

        self.assertEqual(version, jasmin.get_version())


class InterceptionTestCases(RouterPBProxy, RouterPBTestCase):
    @defer.inlineCallbacks
    def test_add_list_and_flush_mt_interceptor(self):
        yield self.connect('127.0.0.1', self.pbPort)

        yield self.mtinterceptor_add(StaticMTInterceptor([GroupFilter(Group(1))], MTInterceptorScript('some code')), 2)
        yield self.mtinterceptor_add(DefaultInterceptor(MTInterceptorScript('some code')), 0)
        listRet1 = yield self.mtinterceptor_get_all()
        listRet1 = pickle.loads(listRet1)

        yield self.mtinterceptor_flush()
        listRet2 = yield self.mtinterceptor_get_all()
        listRet2 = pickle.loads(listRet2)

        self.assertEqual(2, len(listRet1))
        self.assertEqual(0, len(listRet2))

    @defer.inlineCallbacks
    def test_add_list_and_remove_mt_interceptor(self):
        yield self.connect('127.0.0.1', self.pbPort)

        yield self.mtinterceptor_add(StaticMTInterceptor([GroupFilter(Group(1))], MTInterceptorScript('some code')), 2)
        yield self.mtinterceptor_add(DefaultInterceptor(MTInterceptorScript('some code')), 0)
        listRet1 = yield self.mtinterceptor_get_all()
        listRet1 = pickle.loads(listRet1)

        yield self.mtinterceptor_remove(2)
        listRet2 = yield self.mtinterceptor_get_all()
        listRet2 = pickle.loads(listRet2)

        self.assertEqual(2, len(listRet1))
        self.assertEqual(1, len(listRet2))

    @defer.inlineCallbacks
    def test_add_list_and_flush_mo_interceptor(self):
        yield self.connect('127.0.0.1', self.pbPort)

        yield self.mointerceptor_add(DefaultInterceptor(MOInterceptorScript('some code')), 0)
        listRet1 = yield self.mointerceptor_get_all()
        listRet1 = pickle.loads(listRet1)

        yield self.mointerceptor_flush()
        listRet2 = yield self.mointerceptor_get_all()
        listRet2 = pickle.loads(listRet2)

        self.assertEqual(1, len(listRet1))
        self.assertEqual(0, len(listRet2))

    @defer.inlineCallbacks
    def test_add_list_and_remove_mo_interceptor(self):
        yield self.connect('127.0.0.1', self.pbPort)

        yield self.mointerceptor_add(DefaultInterceptor(MOInterceptorScript('some code')), 0)
        listRet1 = yield self.mointerceptor_get_all()
        listRet1 = pickle.loads(listRet1)

        yield self.mtinterceptor_remove(0)
        listRet2 = yield self.mtinterceptor_get_all()
        listRet2 = pickle.loads(listRet2)

        self.assertEqual(1, len(listRet1))
        self.assertEqual(0, len(listRet2))


class InterceptionConnectorTypingCases(RouterPBProxy, RouterPBTestCase):
    """Ensure that mtinterceptor_add and mointerceptor_add methods wont accept invalid scripts,
    for example:
        - mointerceptor_add wont accept an interceptor with a MTInterceptorScript
        - mtinterceptor_add wont accept an interceptor with a MOInterceptorScript
    """

    @defer.inlineCallbacks
    def test_add_mt_interceptor(self):
        yield self.connect('127.0.0.1', self.pbPort)

        r = yield self.mtinterceptor_add(DefaultInterceptor(MOInterceptorScript('some code')), 0)
        self.assertFalse(r)
        r = yield self.mtinterceptor_add(DefaultInterceptor(InterceptorScript('some code')), 0)
        self.assertFalse(r)
        r = yield self.mtinterceptor_add(DefaultInterceptor(MTInterceptorScript('some code')), 0)
        self.assertTrue(r)

    @defer.inlineCallbacks
    def test_add_mo_interceptor(self):
        yield self.connect('127.0.0.1', self.pbPort)

        r = yield self.mointerceptor_add(DefaultInterceptor(MTInterceptorScript('some code')), 0)
        self.assertFalse(r)
        r = yield self.mointerceptor_add(DefaultInterceptor(InterceptorScript('some code')), 0)
        self.assertFalse(r)
        r = yield self.mointerceptor_add(DefaultInterceptor(MOInterceptorScript('some code')), 0)
        self.assertTrue(r)


class RoutingTestCases(RouterPBProxy, RouterPBTestCase):
    @defer.inlineCallbacks
    def test_add_list_and_flush_mt_route(self):
        yield self.connect('127.0.0.1', self.pbPort)

        yield self.mtroute_add(StaticMTRoute([GroupFilter(Group(1))], SmppClientConnector(id_generator()), 0.0), 2)
        yield self.mtroute_add(DefaultRoute(SmppClientConnector(id_generator())), 0)
        listRet1 = yield self.mtroute_get_all()
        listRet1 = pickle.loads(listRet1)

        yield self.mtroute_flush()
        listRet2 = yield self.mtroute_get_all()
        listRet2 = pickle.loads(listRet2)

        self.assertEqual(2, len(listRet1))
        self.assertEqual(0, len(listRet2))

    @defer.inlineCallbacks
    def test_add_list_and_remove_mt_route(self):
        yield self.connect('127.0.0.1', self.pbPort)

        yield self.mtroute_add(StaticMTRoute([GroupFilter(Group(1))], SmppClientConnector(id_generator()), 0.0), 2)
        yield self.mtroute_add(DefaultRoute(SmppClientConnector(id_generator())), 0)
        listRet1 = yield self.mtroute_get_all()
        listRet1 = pickle.loads(listRet1)

        yield self.mtroute_remove(2)
        listRet2 = yield self.mtroute_get_all()
        listRet2 = pickle.loads(listRet2)

        self.assertEqual(2, len(listRet1))
        self.assertEqual(1, len(listRet2))

    @defer.inlineCallbacks
    def test_add_list_and_flush_mo_route_http(self):
        yield self.connect('127.0.0.1', self.pbPort)

        yield self.moroute_add(DefaultRoute(HttpConnector(id_generator(), 'http://127.0.0.1')), 0)
        listRet1 = yield self.moroute_get_all()
        listRet1 = pickle.loads(listRet1)

        yield self.moroute_flush()
        listRet2 = yield self.moroute_get_all()
        listRet2 = pickle.loads(listRet2)

        self.assertEqual(1, len(listRet1))
        self.assertEqual(0, len(listRet2))

    @defer.inlineCallbacks
    def test_add_list_and_remove_mo_route_http(self):
        yield self.connect('127.0.0.1', self.pbPort)

        yield self.moroute_add(DefaultRoute(HttpConnector(id_generator(), 'http://127.0.0.1')), 0)
        listRet1 = yield self.moroute_get_all()
        listRet1 = pickle.loads(listRet1)

        yield self.mtroute_remove(0)
        listRet2 = yield self.mtroute_get_all()
        listRet2 = pickle.loads(listRet2)

        self.assertEqual(1, len(listRet1))
        self.assertEqual(0, len(listRet2))

    @defer.inlineCallbacks
    def test_add_list_and_flush_mo_route_smpps(self):
        yield self.connect('127.0.0.1', self.pbPort)

        yield self.moroute_add(DefaultRoute(SmppServerSystemIdConnector(id_generator())), 0)
        listRet1 = yield self.moroute_get_all()
        listRet1 = pickle.loads(listRet1)

        yield self.moroute_flush()
        listRet2 = yield self.moroute_get_all()
        listRet2 = pickle.loads(listRet2)

        self.assertEqual(1, len(listRet1))
        self.assertEqual(0, len(listRet2))

    @defer.inlineCallbacks
    def test_add_list_and_remove_mo_route_smpps(self):
        yield self.connect('127.0.0.1', self.pbPort)

        yield self.moroute_add(DefaultRoute(SmppServerSystemIdConnector(id_generator())), 0)
        listRet1 = yield self.moroute_get_all()
        listRet1 = pickle.loads(listRet1)

        yield self.mtroute_remove(0)
        listRet2 = yield self.mtroute_get_all()
        listRet2 = pickle.loads(listRet2)

        self.assertEqual(1, len(listRet1))
        self.assertEqual(0, len(listRet2))


class RoutingConnectorTypingCases(RouterPBProxy, RouterPBTestCase):
    """Ensure that mtroute_add and moroute_add methods wont accept invalid connectors,
    for example:
        - moroute_add wont accept a route with a SmppClientConnector
        - mtroute_add wont accept a route with a SmppServerSystemIdConnector
    """

    @defer.inlineCallbacks
    def test_add_mt_route(self):
        yield self.connect('127.0.0.1', self.pbPort)

        r = yield self.mtroute_add(DefaultRoute(HttpConnector(id_generator(), 'http://127.0.0.1')), 0)
        self.assertFalse(r)
        r = yield self.mtroute_add(DefaultRoute(SmppServerSystemIdConnector(id_generator())), 0)
        self.assertFalse(r)
        r = yield self.mtroute_add(DefaultRoute(Connector(id_generator())), 0)
        self.assertFalse(r)
        r = yield self.mtroute_add(DefaultRoute(SmppClientConnector(id_generator())), 0)
        self.assertTrue(r)

    @defer.inlineCallbacks
    def test_add_mo_route(self):
        yield self.connect('127.0.0.1', self.pbPort)

        r = yield self.moroute_add(DefaultRoute(HttpConnector(id_generator(), 'http://127.0.0.1')), 0)
        self.assertTrue(r)
        r = yield self.moroute_add(DefaultRoute(SmppServerSystemIdConnector(id_generator())), 0)
        self.assertTrue(r)
        r = yield self.moroute_add(DefaultRoute(Connector(id_generator())), 0)
        self.assertFalse(r)
        r = yield self.moroute_add(DefaultRoute(SmppClientConnector(id_generator())), 0)
        self.assertFalse(r)


class UserAndGroupTestCases(RouterPBProxy, RouterPBTestCase):
    @defer.inlineCallbacks
    def test_add_user_without_group(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # This group will not be added to router
        g1 = Group(1)

        u1 = User(1, g1, 'username', 'password')
        r = yield self.user_add(u1)
        self.assertEqual(r, False)

    @defer.inlineCallbacks
    def test_authenticate(self):
        yield self.connect('127.0.0.1', self.pbPort)

        g1 = Group(1)
        yield self.group_add(g1)

        u1 = User(1, g1, 'username', 'password')
        yield self.user_add(u1)

        r = yield self.user_authenticate('username', 'password')
        self.assertNotEqual(r, None)
        r = pickle.loads(r)
        self.assertEqual(u1.uid, r.uid)
        self.assertEqual(u1.username, r.username)
        self.assertEqual(u1.password, r.password)
        self.assertEqual(u1.group, g1)

        r = yield self.user_authenticate('username', 'incorrect')
        self.assertEqual(r, None)

        r = yield self.user_authenticate('incorrect', 'password')
        self.assertEqual(r, None)

        r = yield self.user_authenticate('incorrect', 'incorrect')
        self.assertEqual(r, None)

    @defer.inlineCallbacks
    def test_enable_disable_group(self):
        yield self.connect('127.0.0.1', self.pbPort)

        g1 = Group(1)
        yield self.group_add(g1)

        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

        yield self.group_disable(1)
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(False, c[0].enabled)

        yield self.group_enable(1)
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(True, c[0].enabled)

    @defer.inlineCallbacks
    def test_enable_disable_user(self):
        yield self.connect('127.0.0.1', self.pbPort)

        g1 = Group(1)
        yield self.group_add(g1)

        u1 = User(1, g1, 'username', 'password')

        yield self.user_add(u1)
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

        yield self.user_disable(u1.uid)
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(False, c[0].enabled)

        yield self.user_enable(u1.uid)
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(True, c[0].enabled)

    @defer.inlineCallbacks
    def test_add_list_and_remove_group(self):
        yield self.connect('127.0.0.1', self.pbPort)

        g1 = Group(1)
        yield self.group_add(g1)
        g2 = Group(2)
        yield self.group_add(g2)
        g3 = Group(3)
        yield self.group_add(g3)

        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(3, len(c))

        yield self.group_remove(1)
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(2, len(c))

        yield self.group_remove_all()
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))

    @defer.inlineCallbacks
    def test_remove_not_empty_group(self):
        yield self.connect('127.0.0.1', self.pbPort)

        g1 = Group(1)
        yield self.group_add(g1)

        u1 = User(1, g1, 'username1', 'password')
        yield self.user_add(u1)
        u2 = User(2, g1, 'username2', 'password')
        yield self.user_add(u2)

        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(2, len(c))

        yield self.group_remove_all()
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))

        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))

    @defer.inlineCallbacks
    def test_add_list_and_remove_user(self):
        yield self.connect('127.0.0.1', self.pbPort)

        g1 = Group(1)
        yield self.group_add(g1)

        u1 = User(1, g1, 'username', 'password')
        u2 = User(2, g1, 'username2', 'password')

        yield self.user_add(u1)
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

        yield self.user_add(u2)
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(2, len(c))

        yield self.user_remove(u1.uid)
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

        yield self.user_add(u2)
        yield self.user_remove_all()
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))

    @defer.inlineCallbacks
    def test_add_list_user_with_groups(self):
        yield self.connect('127.0.0.1', self.pbPort)

        g1 = Group(1)
        yield self.group_add(g1)

        g2 = Group(2)
        yield self.group_add(g2)

        u1 = User(1, g1, 'username', 'password')
        yield self.user_add(u1)
        u2 = User(2, g2, 'username2', 'password')
        yield self.user_add(u2)

        # Get all users
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(2, len(c))

        # Get users from gid=1
        c = yield self.user_get_all(1)
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

    @defer.inlineCallbacks
    def test_user_unicity(self):
        yield self.connect('127.0.0.1', self.pbPort)

        g1 = Group(1)
        yield self.group_add(g1)

        # Users are unique by uid or username
        # The below 3 samples must be saved as two users
        # the Router will replace a User if it finds the same
        # uid or username
        u1 = User(1, g1, 'username', 'password')
        u2 = User(2, g1, 'username', 'password')
        u3 = User(2, g1, 'other', 'password')

        yield self.user_add(u1)
        yield self.user_add(u2)
        yield self.user_add(u3)
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

        u = yield self.user_authenticate('other', 'password')
        u = pickle.loads(u)
        self.assertEqual(u3.username, u.username)

    @defer.inlineCallbacks
    def test_user_unicity_with_same_CnxStatus(self):
        """When replacing a user with user_add, user.getCnxStatus()
        must not be intiated again.
        """

        yield self.connect('127.0.0.1', self.pbPort)

        g1 = Group(1)
        yield self.group_add(g1)

        # One: add new user
        u1 = User(1, g1, 'username', 'password')
        yield self.user_add(u1)

        # Get CnxStatus
        self.assertEqual(1, len(self.pbRoot_f.users))
        oldCnxStatus = self.pbRoot_f.users[0].getCnxStatus()

        # Two: update password
        u1 = User(1, g1, 'username', 'newpwd')
        yield self.user_add(u1)

        # Get CnxStatus
        self.assertEqual(1, len(self.pbRoot_f.users))
        newCnxStatus = self.pbRoot_f.users[0].getCnxStatus()

        # Asserts
        self.assertEqual(oldCnxStatus, newCnxStatus)


class PersistenceTestCase(RouterPBProxy, RouterPBTestCase):
    @defer.inlineCallbacks
    def tearDown(self):
        # Remove persisted configurations
        filelist = glob.glob("%s/*" % self.RouterPBConfigInstance.store_path)
        for f in filelist:
            if os.path.isfile(f):
                os.remove(f)

        yield RouterPBTestCase.tearDown(self)


class ConfigurationPersistenceTestCases(PersistenceTestCase):
    @defer.inlineCallbacks
    def test_persist_default(self):
        yield self.connect('127.0.0.1', self.pbPort)

        persistRet = yield self.persist()

        self.assertTrue(persistRet)

    @defer.inlineCallbacks
    def test_load_undefined_profile(self):
        yield self.connect('127.0.0.1', self.pbPort)

        loadRet = yield self.load()

        self.assertFalse(loadRet)

    @defer.inlineCallbacks
    def test_add_users_and_groups_persist_and_load_default(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # Add users and groups
        g1 = Group(1)
        yield self.group_add(g1)

        u1 = User(1, g1, 'username', 'password')
        yield self.user_add(u1)
        u2 = User(2, g1, 'username2', 'password')
        yield self.user_add(u2)

        # List users
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(2, len(c))
        # List groups
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

        # Persist
        yield self.persist()

        # Remove all users
        yield self.user_remove_all()

        # List and assert
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))

        # Load
        yield self.load()

        # List users
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(2, len(c))
        # List groups
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

    @defer.inlineCallbacks
    def test_add_all_persist_and_load_default(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # Add users and groups
        g1 = Group(1)
        yield self.group_add(g1)

        u1 = User(1, g1, 'username', 'password')
        yield self.user_add(u1)
        u2 = User(2, g1, 'username2', 'password')
        yield self.user_add(u2)

        # Add mo route
        yield self.moroute_add(DefaultRoute(HttpConnector(id_generator(), 'http://127.0.0.1/any')), 0)

        # Add mt route
        yield self.mtroute_add(DefaultRoute(SmppClientConnector(id_generator())), 0)

        # Add mo interceptor
        yield self.mointerceptor_add(DefaultInterceptor(MOInterceptorScript('some code')), 0)

        # Add mt interceptor
        yield self.mtinterceptor_add(DefaultInterceptor(MTInterceptorScript('some code')), 0)

        # List users
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(2, len(c))
        # List groups
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))
        # List mo routes
        c = yield self.moroute_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))
        # List mt routes
        c = yield self.mtroute_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))
        # List mo interceptors
        c = yield self.mointerceptor_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))
        # List mt interceptors
        c = yield self.mtinterceptor_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

        # Persist
        yield self.persist()

        # Remove all users
        yield self.user_remove_all()
        # Remove all group
        yield self.group_remove_all()
        # Remove all mo routes
        yield self.moroute_flush()
        # Remove all mt routes
        yield self.mtroute_flush()
        # Remove all mo interceptors
        yield self.mointerceptor_flush()
        # Remove all mt interceptors
        yield self.mtinterceptor_flush()

        # List and assert
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))
        # List groups
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))
        # List mo routes
        c = yield self.moroute_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))
        # List mt routes
        c = yield self.mtroute_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))
        # List mo interceptors
        c = yield self.mointerceptor_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))
        # List mt interceptors
        c = yield self.mtinterceptor_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))

        # Load
        yield self.load()

        # List users
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(2, len(c))
        # List groups
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))
        # List mo routes
        c = yield self.moroute_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))
        # List mt routes
        c = yield self.mtroute_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))
        # List mo interceptors
        c = yield self.mointerceptor_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))
        # List mt interceptors
        c = yield self.mtinterceptor_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

    @defer.inlineCallbacks
    def test_add_persist_and_load_profile(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # Add users and groups
        g1 = Group(1)
        yield self.group_add(g1)

        u1 = User(1, g1, 'username', 'password')
        yield self.user_add(u1)
        u2 = User(2, g1, 'username2', 'password')
        yield self.user_add(u2)

        # List users
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(2, len(c))
        # List groups
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

        # Persist
        yield self.persist('profile')

        # Remove all users
        yield self.user_remove_all()

        # List and assert
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))

        # Load
        yield self.load('profile')

        # List users
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(2, len(c))
        # List groups
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

    @defer.inlineCallbacks
    def test_persist_scope_groups(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # Add users and groups
        g1 = Group(1)
        yield self.group_add(g1)

        u1 = User(1, g1, 'username', 'password')
        yield self.user_add(u1)
        u2 = User(2, g1, 'username2', 'password')
        yield self.user_add(u2)

        # List users
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(2, len(c))
        # List groups
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

        # Persist groups only
        yield self.persist(scope='groups')

        # Remove all users
        yield self.user_remove_all()
        # Remove all groups
        yield self.group_remove_all()

        # List users
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))
        # List groups
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))

        # Load
        yield self.load(scope='groups')  # Load with scope=all may also work

        # List users
        c = yield self.user_get_all()
        c = pickle.loads(c)
        self.assertEqual(0, len(c))
        # List groups
        c = yield self.group_get_all()
        c = pickle.loads(c)
        self.assertEqual(1, len(c))

    @defer.inlineCallbacks
    def test_persitance_flag(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # Initially, all config is already persisted
        isPersisted = yield self.is_persisted()
        self.assertTrue(isPersisted)

        # Make config modifications and assert is_persisted()
        g1 = Group(1)
        yield self.group_add(g1)

        # Config is not persisted, waiting for persistance
        isPersisted = yield self.is_persisted()
        self.assertFalse(isPersisted)

        u1 = User(1, g1, 'username', 'password')
        yield self.user_add(u1)

        # Config is not persisted, waiting for persistance
        isPersisted = yield self.is_persisted()
        self.assertFalse(isPersisted)

        u2 = User(2, g1, 'username2', 'password')
        yield self.user_add(u2)

        # Persist
        yield self.persist()

        # Config is now persisted
        isPersisted = yield self.is_persisted()
        self.assertTrue(isPersisted)

        # Add mo route
        yield self.moroute_add(DefaultRoute(HttpConnector(id_generator(), 'http://127.0.0.1/any')), 0)

        # Config is not persisted, waiting for persistance
        isPersisted = yield self.is_persisted()
        self.assertFalse(isPersisted)

        # Add mt route
        yield self.mtroute_add(DefaultRoute(SmppClientConnector(id_generator())), 0)

        # Persist
        yield self.persist()

        # Config is now persisted
        isPersisted = yield self.is_persisted()
        self.assertTrue(isPersisted)

        # Remove all users
        yield self.user_remove_all()
        # Remove all group
        yield self.group_remove_all()
        # Remove all mo routes
        yield self.moroute_flush()
        # Remove all mt routes
        yield self.mtroute_flush()

        # Config is not persisted, waiting for persistance
        isPersisted = yield self.is_persisted()
        self.assertFalse(isPersisted)

        # Load
        yield self.load()

        # Config is now persisted
        isPersisted = yield self.is_persisted()
        self.assertTrue(isPersisted)


class QuotasUpdatedPersistenceTestCases(PersistenceTestCase):
    @defer.inlineCallbacks
    def test_manual_persist_sets_quotas_updated_to_false(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # Add a group
        g1 = Group(1)
        yield self.group_add(g1)

        # Add a user
        mt_c = MtMessagingCredential()
        mt_c.setQuota('balance', 2.0)
        u1 = User(1, g1, 'username', 'password', mt_c)
        yield self.user_add(u1)

        # Config is not persisted, waiting for persistance
        isPersisted = yield self.is_persisted()
        self.assertFalse(isPersisted)

        # Check quotas_updated flag
        self.assertFalse(self.pbRoot_f.users[0].mt_credential.quotas_updated)

        # Update quota and check for quotas_updated
        self.pbRoot_f.users[0].mt_credential.updateQuota('balance', -1.0)
        self.assertTrue(self.pbRoot_f.users[0].mt_credential.quotas_updated)
        self.assertEqual(self.pbRoot_f.users[0].mt_credential.getQuota('balance'), 1)

        # Manual persistence and check for quotas_updated
        persistRet = yield self.persist()
        self.assertTrue(persistRet)
        self.assertFalse(self.pbRoot_f.users[0].mt_credential.quotas_updated)

        # Balance would not change after persistence
        self.assertEqual(self.pbRoot_f.users[0].mt_credential.getQuota('balance'), 1)

    @defer.inlineCallbacks
    def test_manual_load_sets_quotas_updated_to_false(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # Add a group
        g1 = Group(1)
        yield self.group_add(g1)

        # Add a user
        mt_c = MtMessagingCredential()
        mt_c.setQuota('balance', 2.0)
        u1 = User(1, g1, 'username', 'password', mt_c)
        yield self.user_add(u1)

        # Manual persistence and check for quotas_updated
        persistRet = yield self.persist()
        self.assertTrue(persistRet)

        # Config is persisted
        isPersisted = yield self.is_persisted()
        self.assertTrue(isPersisted)

        # Check quotas_updated flag
        self.assertFalse(self.pbRoot_f.users[0].mt_credential.quotas_updated)

        # Update quota and check for quotas_updated
        self.pbRoot_f.users[0].mt_credential.updateQuota('balance', -1.0)
        self.assertTrue(self.pbRoot_f.users[0].mt_credential.quotas_updated)
        self.assertEqual(self.pbRoot_f.users[0].mt_credential.getQuota('balance'), 1)

        # Manual load and check for quotas_updated
        loadRet = yield self.load()
        self.assertTrue(loadRet)
        self.assertFalse(self.pbRoot_f.users[0].mt_credential.quotas_updated)

        # Balance will be reset after persistence
        self.assertEqual(self.pbRoot_f.users[0].mt_credential.getQuota('balance'), 2.0)

    @defer.inlineCallbacks
    def test_automatic_persist_on_quotas_updated(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # Mock perspective_persist for later assertions
        self.pbRoot_f.perspective_persist = Mock(self.pbRoot_f.perspective_persist)
        # Reset persistence_timer_secs to shorten the test time
        self.pbRoot_f.config.persistence_timer_secs = 0.1
        self.pbRoot_f.activatePersistenceTimer()

        # Add a group
        g1 = Group(1)
        yield self.group_add(g1)

        # Add a user
        mt_c = MtMessagingCredential()
        mt_c.setQuota('balance', 2.0)
        u1 = User(1, g1, 'username', 'password', mt_c)
        yield self.user_add(u1)

        # Update quota and check for quotas_updated
        self.pbRoot_f.users[0].mt_credential.updateQuota('balance', -1.0)
        self.assertTrue(self.pbRoot_f.users[0].mt_credential.quotas_updated)
        self.assertEqual(self.pbRoot_f.users[0].mt_credential.getQuota('balance'), 1)

        # Wait 2 seconds for automatic persistence to be done
        yield waitFor(2)

        # assert for 2 calls to persist: 1.users and 2.groups
        self.assertEqual(self.pbRoot_f.perspective_persist.call_count, 2)
        self.assertEqual(self.pbRoot_f.perspective_persist.call_args_list,
                         [call(scope='groups'), call(scope='users')])

    @defer.inlineCallbacks
    def test_increase_decrease_quota(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # Add a group
        g1 = Group(1)
        yield self.group_add(g1)
        # Add a user
        mt_c = MtMessagingCredential()
        mt_c.setQuota('balance', 2.0)
        mt_c.setQuota('submit_sm_count', 10)
        smpps_c = SmppsCredential()
        smpps_c.setQuota('max_bindings', 10)
        u1 = User(1, g1, 'username', 'password', mt_c, smpps_c)
        yield self.user_add(u1)

        # Update quotas
        r = yield self.user_update_quota(1, 'mt_credential', 'balance', -0.2)
        self.assertTrue(r)
        yield self.user_update_quota(1, 'mt_credential', 'balance', +0.5)
        self.assertEqual(self.pbRoot_f.users[0].mt_credential.getQuota('balance'), 2.3)
        r = yield self.user_update_quota(1, 'mt_credential', 'submit_sm_count', -2)
        self.assertTrue(r)
        yield self.user_update_quota(1, 'mt_credential', 'submit_sm_count', +5)
        self.assertEqual(self.pbRoot_f.users[0].mt_credential.getQuota('submit_sm_count'), 13)
        r = yield self.user_update_quota(1, 'smpps_credential', 'max_bindings', -2)
        self.assertTrue(r)
        yield self.user_update_quota(1, 'smpps_credential', 'max_bindings', +5)
        self.assertEqual(self.pbRoot_f.users[0].smpps_credential.getQuota('max_bindings'), 13)

        # Set quotas (the same as update, but value is replaced instead of incremented)
        r = yield self.user_set_quota(1, 'mt_credential', 'balance', 300)
        self.assertTrue(r)
        yield self.user_set_quota(1, 'mt_credential', 'balance', 200)
        self.assertEqual(self.pbRoot_f.users[0].mt_credential.getQuota('balance'), 200)
        r = yield self.user_set_quota(1, 'mt_credential', 'submit_sm_count', 300)
        self.assertTrue(r)
        yield self.user_set_quota(1, 'mt_credential', 'submit_sm_count', 200)
        self.assertEqual(self.pbRoot_f.users[0].mt_credential.getQuota('submit_sm_count'), 200)
        r = yield self.user_set_quota(1, 'smpps_credential', 'max_bindings', 10)
        self.assertTrue(r)
        yield self.user_set_quota(1, 'smpps_credential', 'max_bindings', 10)
        self.assertEqual(self.pbRoot_f.users[0].smpps_credential.getQuota('max_bindings'), 10)

    @defer.inlineCallbacks
    def test_increase_decrease_quota_invalid_cred(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # Add a group
        g1 = Group(1)
        yield self.group_add(g1)
        # Add a user
        u1 = User(1, g1, 'username', 'password')
        yield self.user_add(u1)

        # Update quotas
        r = yield self.user_update_quota(1, 'any_cred', 'balance', -0.2)
        self.assertFalse(r)

    @defer.inlineCallbacks
    def test_increase_decrease_quota_invalid_type(self):
        yield self.connect('127.0.0.1', self.pbPort)

        # Add a group
        g1 = Group(1)
        yield self.group_add(g1)
        # Add a user
        mt_c = MtMessagingCredential()
        mt_c.setQuota('submit_sm_count', 10)
        u1 = User(1, g1, 'username', 'password', mt_c)
        yield self.user_add(u1)

        # Update quotas
        r = yield self.user_update_quota(1, 'mt_credential', 'submit_sm_count', -2.2)
        self.assertFalse(r)
        self.assertEqual(self.pbRoot_f.users[0].mt_credential.getQuota('submit_sm_count'), 10)


class SimpleNonConnectedSubmitSmDeliveryTestCases(RouterPBProxy, SMPPClientManagerPBTestCase):
    @defer.inlineCallbacks
    def test_delivery(self):
        yield self.connect('127.0.0.1', self.pbPort)

        g1 = Group(1)
        yield self.group_add(g1)

        c1 = SmppClientConnector(id_generator())
        u1_password = 'password'
        u1 = User(1, g1, 'username', u1_password)
        u2_password = 'password'
        u2 = User(1, g1, 'username2', u2_password)
        yield self.user_add(u1)

        yield self.mtroute_add(DefaultRoute(c1), 0)

        # Send a SMS MT through http interface
        url_ko = 'http://127.0.0.1:1401/send'
        ko_params = {
            'to': '06155423',
            'content': 'test',
            'username': u2.username,
            'password': u1_password
        }
        url_ok = 'http://127.0.0.1:1401/send'
        ok_params = {
            'to': '06155423',
            'content': 'test',
            'username': u1.username,
            'password': u2_password
        }
        # Incorrect username/password will lead to '403 Forbidden' error
        agent = Agent(reactor)
        client = HTTPClient(agent)
        response = yield client.post(url_ko, data=ko_params)

        lastErrorStatus = response.code

        self.assertEqual(lastErrorStatus, 403)

        # Since Connector doesnt really exist, the message will not be routed
        # to a queue, a 500 error will be returned, and more details will be written
        # in smpp client manager log:
        # 'Trying to enqueue a SUBMIT_SM to a connector with an unknown cid: '
        agent = Agent(reactor)
        client = HTTPClient(agent)
        response = yield client.post(url_ok, data=ok_params)

        lastErrorStatus = response.code

        self.assertEqual(lastErrorStatus, 500)

        # Now we'll create the connecter and send an MT to it
        yield self.SMPPClientManagerPBProxy.connect('127.0.0.1', self.CManagerPort)
        c1Config = SMPPClientConfig(id=c1.cid)
        yield self.SMPPClientManagerPBProxy.add(c1Config)

        # We should receive a msg id
        agent = Agent(reactor)
        client = HTTPClient(agent)
        response = yield client.post(url_ok, data=ok_params)
        c = yield text_content(response)
        self.assertEqual(c[:7], 'Success')
        # @todo: Should be a real uuid pattern testing
        self.assertApproximates(len(c), 40, 10)


class LastClientFactory(Factory):
    lastClient = None

    def buildProtocol(self, addr):
        self.lastClient = Factory.buildProtocol(self, addr)
        return self.lastClient


class HappySMSCTestCase(SMPPClientManagerPBTestCase):
    protocol = ManualDeliveryReceiptHappySMSC

    @defer.inlineCallbacks
    def setUp(self, interceptorpb_client=None):
        yield SMPPClientManagerPBTestCase.setUp(self, interceptorpb_client)

        self.smsc_f = LastClientFactory()
        self.smsc_f.protocol = self.protocol
        self.SMSCPort = reactor.listenTCP(0, self.smsc_f)

    @defer.inlineCallbacks
    def tearDown(self):
        yield self.SMSCPort.stopListening()
        yield SMPPClientManagerPBTestCase.tearDown(self)


class SubmitSmTestCaseTools:
    """
    Factorized methods for child classes testing SubmitSm and DeliverSm routing scenarios
    """

    @defer.inlineCallbacks
    def prepareRoutingsAndStartConnector(self, reconnectOnConnectionLoss=True, bindOperation='transceiver',
                                         route_rate=0.0, user=None, port=None, dlr_msg_id_bases=0,
                                         source_addr_ton=AddrTon.NATIONAL, source_addr_npi=AddrNpi.ISDN,
                                         dest_addr_ton=AddrTon.INTERNATIONAL, dest_addr_npi=AddrNpi.ISDN,
                                         route=None, route_order=1, c1=None):
        # Routing stuff
        g1 = Group(1)
        yield self.group_add(g1)

        user_password = 'password'
        if user is None:
            self.u1 = User(1, g1, 'username', user_password)
        else:
            self.u1 = user
        yield self.user_add(self.u1)

        if c1 is None:
            self.c1 = SmppClientConnector(id_generator())
        else:
            self.c1 = c1

        if route is None:
            yield self.mtroute_add(DefaultRoute(self.c1, route_rate), 0)
        else:
            yield self.mtroute_add(route, route_order)

        # Set port
        if port is None:
            port = self.SMSCPort.getHost().port

        # Now we'll create the connecter
        yield self.SMPPClientManagerPBProxy.connect('127.0.0.1', self.CManagerPort)
        c1Config = SMPPClientConfig(id=self.c1.cid, port=port,
                                    reconnectOnConnectionLoss=reconnectOnConnectionLoss,
                                    responseTimerSecs=1,
                                    bindOperation=bindOperation,
                                    dlr_msg_id_bases=dlr_msg_id_bases,
                                    source_addr_ton=source_addr_ton,
                                    source_addr_npi=source_addr_npi,
                                    dest_addr_ton=dest_addr_ton,
                                    dest_addr_npi=dest_addr_npi,
                                    )
        yield self.SMPPClientManagerPBProxy.add(c1Config)

        # Start the connector
        yield self.SMPPClientManagerPBProxy.start(self.c1.cid)
        # Wait for 'BOUND_TRX' state
        while True:
            ssRet = yield self.SMPPClientManagerPBProxy.session_state(self.c1.cid)
            if ssRet[:6] == 'BOUND_':
                break
            else:
                yield waitFor(0.2)

        # Configuration
        self.method = 'GET'
        self.postdata = None
        self.params = {'to': '06155423',
                       'username': self.u1.username,
                       'password': user_password,
                       'content': 'test'}

        if hasattr(self, 'AckServer'):
            # Send a SMS MT through http interface and set delivery receipt callback in url
            self.dlr_url = 'http://127.0.0.1:%d/receipt' % (self.AckServer.getHost().port)

            self.AckServerResource.render_POST = Mock(wraps=self.AckServerResource.render_POST)
            self.AckServerResource.render_GET = Mock(wraps=self.AckServerResource.render_GET)

    @defer.inlineCallbacks
    def stopSmppClientConnectors(self):
        # Disconnect the connector
        yield self.SMPPClientManagerPBProxy.stop(self.c1.cid)
        # Wait for 'BOUND_TRX' state
        while True:
            ssRet = yield self.SMPPClientManagerPBProxy.session_state(self.c1.cid)
            if ssRet == 'NONE' or ssRet == 'UNBOUND':
                break
            else:
                yield waitFor(0.2)


class NoSubmitSmWhenReceiverIsBoundSMSCTestCases(SMPPClientManagerPBTestCase):
    protocol = NoSubmitSmWhenReceiverIsBoundSMSC

    @defer.inlineCallbacks
    def setUp(self):
        yield SMPPClientManagerPBTestCase.setUp(self)

        self.smsc_f = LastClientFactory()
        self.smsc_f.protocol = self.protocol
        self.SMSCPort = reactor.listenTCP(0, self.smsc_f)

    @defer.inlineCallbacks
    def tearDown(self):
        yield SMPPClientManagerPBTestCase.tearDown(self)

        yield self.SMSCPort.stopListening()


class BOUND_RX_SubmitSmTestCases(RouterPBProxy, NoSubmitSmWhenReceiverIsBoundSMSCTestCases, SubmitSmTestCaseTools):
    @defer.inlineCallbacks
    def setUp(self):
        yield NoSubmitSmWhenReceiverIsBoundSMSCTestCases.setUp(self)

        # Start http servers
        self.AckServerResource = AckServer()
        self.AckServer = reactor.listenTCP(0, server.Site(self.AckServerResource))

    @defer.inlineCallbacks
    def tearDown(self):
        yield NoSubmitSmWhenReceiverIsBoundSMSCTestCases.tearDown(self)

        yield self.AckServer.stopListening()

    @defer.inlineCallbacks
    def test_delivery_using_incorrectly_bound_connector(self):
        yield self.connect('127.0.0.1', self.pbPort)
        yield self.prepareRoutingsAndStartConnector(bindOperation='receiver')

        self.params['dlr-url'] = self.dlr_url
        self.params['dlr-level'] = 1
        baseurl = 'http://127.0.0.1:1401/send'

        # Send a MT
        agent = Agent(reactor)
        client = HTTPClient(agent)
        response = yield client.post(baseurl, data=self.params)
        c = yield text_content(response)
        msgStatus = c[:7]
        msgId = c[9:45]

        # Wait 1 seconds for submit_sm_resp
        yield waitFor(1)

        yield self.stopSmppClientConnectors()

        # Run tests
        self.assertEqual(msgStatus, 'Success')
        # A DLR must be sent to dlr_url
        self.assertEqual(self.AckServerResource.render_POST.call_count, 1)
        # Message ID must be transmitted in the DLR
        callArgs = self.AckServerResource.render_POST.call_args_list[0][0][0].args
        self.assertEqual(callArgs[b'id'][0], msgId.encode())
        self.assertEqual(callArgs[b'message_status'][0], b'ESME_RINVBNDSTS')


class BillRequestSubmitSmRespCallbackingTestCases(RouterPBProxy, HappySMSCTestCase, SubmitSmTestCaseTools):
    @defer.inlineCallbacks
    def test_unrated_route(self):
        yield self.connect('127.0.0.1', self.pbPort)
        yield self.prepareRoutingsAndStartConnector()

        self.params['content'] = composeMessage({'_'}, 200)
        baseurl = 'http://127.0.0.1:1401/send'

        # Send a MT
        agent = Agent(reactor)
        client = HTTPClient(agent)
        response = yield client.post(baseurl, data=self.params)

        # Wait 1 seconds for submit_sm_resp
        yield waitFor(1)

        yield self.stopSmppClientConnectors()

        # Run tests
        # Unrated route will not callback, nothing to bill
        self.assertEqual(self.pbRoot_f.bill_request_submit_sm_resp_callback.call_count, 0)

    @defer.inlineCallbacks
    def test_rated_route(self):
        yield self.connect('127.0.0.1', self.pbPort)
        mt_c = MtMessagingCredential()
        mt_c.setQuota('balance', 2.0)
        mt_c.setQuota('early_decrement_balance_percent', 10)
        user = User(1, Group(1), 'username', 'password', mt_c)
        yield self.prepareRoutingsAndStartConnector(route_rate=1.0, user=user)

        self.params['content'] = composeMessage({'_'}, 10)
        baseurl = 'http://127.0.0.1:1401/send'

        # Send a MT
        agent = Agent(reactor)
        client = HTTPClient(agent)
        response = yield client.post(baseurl, data=self.params)

        # Wait 1 seconds for submit_sm_resp
        yield waitFor(1)

        yield self.stopSmppClientConnectors()

        # Run tests
        # Rated route will callback with a bill
        self.assertEqual(self.pbRoot_f.bill_request_submit_sm_resp_callback.call_count, 1)
