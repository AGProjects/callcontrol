# Copyright (C) 2005-2008 AG Projects. See LICENSE for details.
#

"""Rating engine interface implementation."""

import random
from collections import deque

from application.configuration import ConfigSection, ConfigFile
from application.configuration.datatypes import EndpointAddress
from application import log
from application.python.util import Singleton

from twisted.internet.protocol import ReconnectingClientFactory
from twisted.internet.error import TimeoutError
from twisted.internet import reactor, defer
from twisted.protocols.basic import LineOnlyReceiver
from twisted.protocols.policies import TimeoutMixin
from twisted.python import failure

from callcontrol import configuration_filename

##
## Rating engine configuration
##
class RatingEngineAddress(EndpointAddress):
    _defaultPort = 9024
    _name = 'rating engine address'

class RatingEngineAddresses(list):
    def __new__(cls, engines):
        engines = engines.split()
        engines = [RatingEngineAddress(engine) for engine in engines]
        return engines

class RatingConfig(ConfigSection):
    _datatypes = {'address': RatingEngineAddresses}
    address = [('127.0.0.1', 9024)]
    timeout = 500

## We use this to overwrite some of the settings above on a local basis if needed
config_file = ConfigFile(configuration_filename)
config_file.read_settings('CDRTool', RatingConfig)


class RatingError(Exception): pass
class RatingEngineError(RatingError): pass
class RatingEngineTimeoutError(TimeoutError): pass

class RatingRequest(str):
    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.deferred = defer.Deferred()
    def __new__(cls, command, **kwargs):
        reqstr = command + (kwargs and (' ' + ' '.join("%s=%s" % (name,value) for name, value in kwargs.items())) or '')
        obj = str.__new__(cls, reqstr)
        return obj

class RatingEngineProtocol(LineOnlyReceiver):
    delimiter = '\n\n'
    def __init__(self):
        self.connected = False
        self.__request = None
        self.__request_queue = deque()
        self.__timeout_call = None
    
    def connectionMade(self):
        log.info("Connected to Rating Engine at %s:%d" % (self.transport.getPeer().host, self.transport.getPeer().port))
        self.connected = True
        self.factory.application.connectionMade(self.transport.connector)

    def connectionLost(self, reason=None):
        log.info("Disconnected from Rating Engine at %s:%d" % (self.transport.getPeer().host, self.transport.getPeer().port))
        self.connected = False
        if self.__request:
            self._respond("Connection with the Rating Engine is down: %s" % reason, success=False)

    def timeoutConnection(self):
        log.info("Connection to Rating Engine at %s:%d timedout" % (self.transport.getPeer().host, self.transport.getPeer().port))
        self.transport.loseConnection(RatingEngineTimeoutError())

    def lineReceived(self, line):
#        log.debug("Got reply from rating engine: %s" % line) #DEBUG
        if not line:
            return
        if self.__timeout_call is not None:
            self.__timeout_call.cancel()
        if self.__request is None:
            log.warn("Got reply for unexisting request: %s" % line)
            return
        def _unknown_handler(line):
            self._respond("Unknown command in request. Cannot handle reply. Reply is: %s" % line, success=False)
        try:
            self._respond(getattr(self, '_PE_%s' % self.__request.command.lower(), _unknown_handler)(line))
        except Exception, e:
            self._respond(str(e), success=False)

    def _PE_maxsessiontime(self, line):
        try:
            limit = line.splitlines()[0].strip().capitalize()
            try:
                limit = int(limit)
            except:
                if limit == 'None':
                    limit = None
                elif limit == 'Locked':
                    pass
                else:
                    raise ValueError, "limit must be a positive number, None or Locked"
            else:
                if limit < 0:
                    raise ValueError, "limit must be a positive number, None or Locked"
        except Exception, e:
            raise e
        return limit

    def _PE_debitbalance(self, line):
        valid_answers = ('Ok', 'Failed', 'Not prepaid')
        lines = line.splitlines()
        result = lines[0].strip().capitalize()
        if result not in valid_answers:
            log.error("Invalid reply from rating engine: `%s'" % result)
            log.warn("Rating engine possible failed query: %s" % cmd)
        elif result == 'Failed':
            log.warn("Rating engine failed query: %s" % cmd)
        totalcost = lines[1].strip()
        return totalcost


    def _send_next_request(self):
        self.__request = self.__request_queue.popleft()
        if self.connected:
            self.sendLine(self.__request)
            self._set_timeout()
#            log.debug("Sent request to rating engine: %s" % self.__request) #DEBUG
        else:
            self._respond('Connection with the Rating Engine is down', success=False)

    def _respond(self, result, success=True):
        req = self.__request
        self.__request = None
        if success:
            req.deferred.callback(result)
        else:
            req.deferred.errback(failure.Failure(RatingEngineError(result)))
        if self.__request_queue:
            self._send_next_request()

    def _set_timeout(self, timeout=None):
        if timeout is None:
            timeout = self.factory.timeout
        self.__timeout_call = reactor.callLater(timeout/1000.0, self.timeoutConnection)

    def send_request(self, request):
        self.__request_queue.append(request)
        if self.__request is None:
            self._send_next_request()
        return request


class RatingEngineFactory(ReconnectingClientFactory):
    protocol = RatingEngineProtocol

    timeout = RatingConfig.timeout

    # reconnect parameters
    maxDelay = 15
    factor = maxDelay
    initialDelay = 1.0/factor
    delay = initialDelay

    def __init__(self, application):
        self.application = application

    def buildProtocol(self, addr):
        self.resetDelay()
        return ReconnectingClientFactory.buildProtocol(self, addr)

    def clientConnectionFailed(self, connector, reason):
        if self.application.disconnecting:
            return
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)

    def clientConnectionLost(self, connector, reason):
        self.application.connectionLost(connector, reason)
        if self.application.disconnecting:
            return
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)


class RatingEngine(object):
    def __init__(self, address):
        self.address = address
        self.disconnecting = False
        self.connector = reactor.connectTCP(self.address[0], self.address[1], factory=RatingEngineFactory(self))
        self.connection = None

    def shutdown(self):
        self.disconnecting = True
        self.connector.disconnect()

    def connectionMade(self, connector):
        self.connection = connector.transport

    def connectionLost(self, connector, reason):
        self.connection = None
    
    def getCallLimit(self, call):
        if self.connection is not None:
            req = RatingRequest('MaxSessionTime', CallId=call.callid, From=call.billingParty, To=call.ruri,
                          Gateway=call.sourceip, Duration=36000, Lock=1)
            return self.connection.protocol.send_request(req).deferred
        return defer.fail(failure.Failure(RatingEngineError('Connection with Rating Engine is down')))
    
    def debitBalance(self, call):
        if self.connection is not None:
            req = RatingRequest('DebitBalance', CallId=call.callid, From=call.billingParty, To=call.ruri,
                          Gateway=call.sourceip, Duration=call.duration)
            return self.connection.protocol.send_request(req).deferred
        return defer.fail(failure.Failure(RatingEngineError('Connection with Rating Engine is down')))


class RatingEngineConnections(object):
    __metaclass__ = Singleton

    def __init__(self):
        self.connections = [RatingEngine(engine) for engine in RatingConfig.address]
    @staticmethod
    def getConnection():
        conn = RatingEngineConnections()
        return random.choice(conn.connections)

    def shutdown(self):
        for engine in self.connections:
            engine.shutdown()
