# Copyright (C) 2005-2008 AG Projects.
#

"""Prepaid engine interface implementation."""

import random
from collections import deque

from application.configuration import ConfigSection, ConfigFile
from application.configuration.datatypes import EndpointAddress
from application import log
from application.python.util import Singleton

from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.protocols.policies import TimeoutMixin
from twisted.internet import reactor, defer
from twisted.python import failure

from callcontrol import configuration_filename

##
## Prepaid configuration
##
class PrepaidEngineAddress(EndpointAddress):
    _defaultPort = 9024
    _name = 'rating engine address'

class PrepaidConfig(ConfigSection):
    _dataTypes = {'address': PrepaidEngineAddress}
    address = ('127.0.0.1', 9024)
    connections = 1

## We use this to overwrite some of the settings above on a local basis if needed
config_file = ConfigFile(configuration_filename)
config_file.read_settings('PrepaidEngine', PrepaidConfig)


class PrepaidError(Exception): pass
class PrepaidEngineError(PrepaidError): pass

class Request(str):
    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.deferred = defer.Deferred()
    def __new__(cls, command, **kwargs):
        reqstr = command + (kwargs and (' ' + ' '.join("%s=%s" % (name,value) for name, value in kwargs.items())) or '') + '\n'
        obj = str.__new__(cls, reqstr)
        return obj

class PrepaidEngineProtocol(LineOnlyReceiver, TimeoutMixin):
    def __init__(self):
        self.connected = False
        self.__request = None
        self.__request_queue = deque()
    
    def connectionMade(self):
        self.connected = True

    def connectionLost(self):
        self.connected = False
        if self.__request:
            self._respond('Connection with the Prepaid Engine is down', success=False)

    def timeoutConnection(self):
        self.transport.loseConnection()

    def lineReceived(self, line):
        if self.__request is None:
            log.warn("Got %s reply for unexisting request: %s" % (success and 'successful' or 'failure', msg))
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
        except:
            log.error("invalid reply from rating engine: `%s'" % res)
            limit = 0
        return limit

    def _PE_debitbalance(self, line):
        validAnswers = ('Ok', 'Failed', 'Not prepaid')
        result = line.splitlines()[0].strip().capitalize()
        if result not in validAnswers:
            log.error("invalid reply from rating engine: `%s'" % result)
            log.warning("rating engine possible failed query: %s" % cmd)
        elif result == 'Failed':
            log.warning("rating engine failed query: %s" % cmd)

    def _send_next_request(self):
        self.__request = self.__request_queue.popleft()
        if self.connected:
            self.sendLine(self.__request)
            self.setTimeout(self.factory.timeout)
        else:
            self._respond('Connection with the Prepaid Engine is down', success=False)

    def _respond(self, result, success=True):
        self.setTimeout(None)
        self.__request = None
        if success:
            self.__request.deferred.callback(result)
        else:
            self.__request.deferred.errback(failure.Failure(PrepaidEngineError(result)))
        if self.__request_queue:
            self._send_next_request()

    def send_request(self, request):
        self.__request_queue.append(request)
        if self.__request is None:
            self._send_next_request()
        return request


class PrepaidEngineFactory(ReconnectingClientFactory):
    protocol = PrepaidEngineProtocol

    timeout = 3

    # reconnect parameters
    maxDelay = 15
    factor = maxDelay
    initialDelay = 1.0/factor
    delay = initialDelay

    def __init__(self, engine):
        self.engine = engine
        self.proto = None

    def buildProtocol(self, addr):
        self.resetDelay()
        self.proto = ReconnectingClientFactory.buildProtocol(self, addr)
        return self.proto

    def clientConnectionFailed(self, connector, reason):
        self.proto = None
        if self.engine.disconnecting:
            return
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)

    def clientConnectionLost(self, connector, reason):
        self.proto = None
        if self.engine.disconnecting:
            return
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)


class PrepaidEngine(object):
    __metaclass__ = Singleton

    def __init__(self, address=None):
        self.address = address or PrepaidConfig.address
        self.factory = PrepaidEngineFactory(self)
        reactor.connectTCP(factory=self.factory, *self.address)
    
    def getCallLimit(self, call):
        req = Request('MaxSessionTime', CallId=call.callid, From=call.billingParty, To=call.ruri,
                      Gateway=call.sourceip, Duration=36000, Lock=1)
        if self.factory.proto is not None:
            return self.factory.proto.send_request(req).deferred
        return defer.succeed(0)
    
    def debitBalance(self, call):
        req = Request('DebitBalance', CallId=call.callid, From=call.billingParty, To=call.ruri,
                      Gateway=call.sourceip, Duration=call.duration)
        if self.factory.proto is not None:
            return self.factory.proto.send_request(req).deferred
        return defer.succeed(None)
