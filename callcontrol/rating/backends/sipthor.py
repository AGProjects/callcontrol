
from application.version import Version
from application.configuration import ConfigSection, ConfigSetting
from application.system import host
from application import log
from application.python.types import Singleton

from gnutls.interfaces.twisted import TLSContext, X509Credentials

from thor import __version__ as thor_version
from thor.eventservice import EventServiceClient, ThorEvent
from thor.entities import ThorEntitiesRoleMap, GenericThorEntity as ThorEntity

from callcontrol.tls import Certificate, PrivateKey
from callcontrol.rating import RatingEngine, RatingEngineAddress
from callcontrol import configuration_file, __version__

from twisted.internet import defer, reactor

if Version.parse(thor_version) < Version.parse('1.1.21'):
    raise RuntimeError('Thor version is smaller than 1.1.21 (%s)' % thor_version)


class ThorNodeConfig(ConfigSection):
    __cfgfile__ = configuration_file
    __section__ = 'ThorNetwork'

    enabled = False
    domain = "sipthor.net"
    multiply = 1000
    certificate = ConfigSetting(type=Certificate, value=None)
    private_key = ConfigSetting(type=PrivateKey, value=None)
    ca = ConfigSetting(type=Certificate, value=None)


class CallcontrolNode(EventServiceClient):
    __metaclass__ = Singleton
    topics = ["Thor.Members"]

    def __init__(self):
        self.node = ThorEntity(host.default_ip, ['call_control'], version=__version__)
        self.networks = {}
        self.rating_connections = {}
        self.presence_message = ThorEvent('Thor.Presence', self.node.id)
        self.shutdown_message = ThorEvent('Thor.Leave', self.node.id)
        credentials = X509Credentials(ThorNodeConfig.certificate, ThorNodeConfig.private_key, [ThorNodeConfig.ca])
        credentials.verify_peer = True
        tls_context = TLSContext(credentials)
        EventServiceClient.__init__(self, ThorNodeConfig.domain, tls_context)

    def publish(self, event):
        self._publish(event)

    def stop(self):
        return self._shutdown()

    def connectionLost(self, connector, reason):
        """Called when an event server connection goes away"""
        self.connections.discard(connector.transport)

    def connectionFailed(self, connector, reason):
        """Called when an event server connection has an unrecoverable error"""
        connector.failed = True

    def _disconnect_all(self, result):
        for conn in self.connectors:
            conn.disconnect()

    def _shutdown(self):
        if self.disconnecting:
            return
        self.disconnecting = True
        self.dns_monitor.cancel()
        if self.advertiser:
            self.advertiser.cancel()
        if self.shutdown_message:
            self._publish(self.shutdown_message)
        requests = [conn.protocol.unsubscribe(*self.topics) for conn in self.connections]
        d = defer.DeferredList([request.deferred for request in requests])
        d.addCallback(self._disconnect_all)
        return d

    def handle_event(self, event):
        reactor.callFromThread(self._handle_event, event)

    def _handle_event(self, event):
        networks = self.networks
        role_map = ThorEntitiesRoleMap(event.message)  # mapping between role names and lists of nodes with that role
        role = 'rating_server'
        try:
            network = networks[role]
        except KeyError:
            from thor import network as thor_network
            network = thor_network.new(ThorNodeConfig.multiply)
            networks[role] = network
        new_nodes = set([node.ip for node in role_map.get(role, [])])
        old_nodes = set(network.nodes)
        added_nodes = new_nodes - old_nodes
        removed_nodes = old_nodes - new_nodes
        if added_nodes:
            for node in added_nodes:
                network.add_node(node)
                address = RatingEngineAddress(node)
                self.rating_connections[address] = RatingEngine(address)
            plural = 's' if len(added_nodes) != 1 else ''
            log.msg("Added rating node%s: %s" % (plural, ', '.join(added_nodes)))
        if removed_nodes:
            for node in removed_nodes:
                network.remove_node(node)
                address = RatingEngineAddress(node)
                self.rating_connections[address].shutdown()
                del self.rating_connections[address]
            plural = 's' if len(removed_nodes) != 1 else ''
            log.msg("Removed rating node%s: %s" % (plural, ', '.join(removed_nodes)))


class SipthorBackend(object):

    def __init__(self):
        self.node = CallcontrolNode()

    @property
    def connections(self):
        return self.node.rating_connections.values()

    def shutdown(self):
        for connection in self.connections:
            connection.shutdown()
        return self.node.stop()
