
import socket

from application.configuration import ConfigSection, ConfigSetting
from application.system import host
from application import log

from callcontrol import configuration_file
from callcontrol.rating import RatingEngine, RatingEngineAddress

##
## Rating engine configuration
##


class RatingEngineAddresses(list):
    def __new__(cls, engines):
        engines = engines.split()
        engines = [RatingEngineAddress(engine) for engine in engines]
        return engines


class RatingConfig(ConfigSection):
    __cfgfile__ = configuration_file
    __section__ = 'CDRTool'

    address = ConfigSetting(type=RatingEngineAddresses, value=[])
    timeout = 500


class OpensipsBackend(object):

    def __init__(self):
        self.connections = []
        if not RatingConfig.address:
            try:
                RatingConfig.address = RatingEngineAddresses('cdrtool.' + socket.gethostbyaddr(host.default_ip)[0].split('.', 1)[1])
            except Exception:
                log.critical('Cannot resolve hostname %s' % ('cdrtool.' + socket.gethostbyaddr(host.default_ip)[0].split('.', 1)[1]))

        for engine in RatingConfig.address:
            self.connections.append(RatingEngine(engine))

    def shutdown(self):
        for connection in self.connections:
            connection.shutdown()


