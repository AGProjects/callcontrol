# Copyright (C) 2005-2008 AG Projects. See LICENSE for details.
#

"""Rating engine interface implementation."""

import random
import socket
from collections import deque

from application.configuration import ConfigSection, ConfigSetting
from application.configuration.datatypes import EndpointAddress
from application.system import default_host_ip
from application import log
from application.python.util import Singleton

from twisted.internet.protocol import ReconnectingClientFactory
from twisted.internet.error import TimeoutError
from twisted.internet import reactor, defer
from twisted.protocols.basic import LineOnlyReceiver
from twisted.python import failure

from callcontrol import configuration_filename

##
## Rating engine configuration
##
class RatingEngineAddress(EndpointAddress):
    default_port = 9024
    name = 'rating engine address'

class RatingEngineAddresses(list):
    def __new__(cls, engines):
        engines = engines.split()
        engines = [RatingEngineAddress(engine) for engine in engines]
        return engines

class TimeLimit(int):
    """A positive time limit (in seconds) or None"""
    def __new__(typ, value):
        if value.lower() == 'none':
            return None
        try:
            limit = int(value)
        except:
            raise ValueError("invalid time limit value: %r" % value)
        if limit < 0:
            raise ValueError("invalid time limit value: %r. should be positive." % value)
        return limit

class RatingConfig(ConfigSection):
    __cfgfile__ = configuration_filename
    __section__ = 'CDRTool'
    address = ConfigSetting(type=RatingEngineAddresses, value=[])
    timeout = 500

class CallControlConfig(ConfigSection):
    __cfgfile__ = configuration_filename
    __section__ = 'CallControl'
    prepaid_limit = ConfigSetting(type=TimeLimit, value=None)
    limit = ConfigSetting(type=TimeLimit, value=None)

if not RatingConfig.address:
    try:
        RatingConfig.address = RatingEngineAddresses('cdrtool.' + socket.gethostbyaddr(default_host_ip)[0].split('.', 1)[1])
    except Exception, e:
        log.fatal('Cannot resolve hostname %s' % ('cdrtool.' + socket.gethostbyaddr(default_host_ip)[0].split('.', 1)[1]))


class RatingError(Exception): pass
class RatingEngineError(RatingError): pass
class RatingEngineTimeoutError(TimeoutError): pass

class RatingRequest(str):
    def __init__(self, command, reliable=True, **kwargs):
        self.command = command
        self.reliable = reliable
        self.kwargs = kwargs
        self.deferred = defer.Deferred()
    def __new__(cls, command, reliable=True, **kwargs):
        reqstr = command + (kwargs and (' ' + ' '.join("%s=%s" % (name,value) for name, value in kwargs.items())) or '')
        obj = str.__new__(cls, reqstr)
        return obj

class RatingEngineProtocol(LineOnlyReceiver):
    delimiter = '\n\n'
    def __init__(self):
        self.connected = False
        self.__request = None
        self.__timeout_call = None
        self._request_queue = deque()
    
    def connectionMade(self):
        log.info("Connected to Rating Engine at %s:%d" % (self.transport.getPeer().host, self.transport.getPeer().port))
        self.connected = True
        self.factory.application.connectionMade(self.transport.connector)
        if self._request_queue:
            self._send_next_request()

    def connectionLost(self, reason=None):
        log.info("Disconnected from Rating Engine at %s:%d" % (self.transport.getPeer().host, self.transport.getPeer().port))
        self.connected = False
        if self.__request is not None:
            if self.__request.reliable:
                self._request_queue.appendleft(self.__request)
                self.__request = None
            else:
                self._respond("Connection with the Rating Engine is down: %s" % reason, success=False)
        self.factory.application.connectionLost(self.transport.connector, reason, self)

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
        try:
            self._respond(getattr(self, '_PE_%s' % self.__request.command.lower())(line))
        except AttributeError:
            self._respond("Unknown command in request. Cannot handle reply. Reply is: %s" % line, success=False)
        except Exception, e:
            self._respond(str(e), success=False)

    def _PE_maxsessiontime(self, line):
        lines = line.splitlines()

        limit = lines[0].strip().capitalize()
        try:
            limit = int(limit)
        except:
            if limit == 'None':
                limit = None
            elif limit == 'Locked':
                pass
            else:
                raise ValueError("limit must be a non-negative number, None or Locked: %s" % limit)
        else:
            if limit < 0:
                raise ValueError("limit must be a non-negative number, None or Locked: %s" % limit)

        info = dict(line.split('=', 1) for line in lines[1:])
        if 'type' in info:
            type = info['type'].lower()
            if type == 'prepaid':
                prepaid = True
            elif type == 'postpaid':
                prepaid = False
            else:
                raise ValueError("prepaid must be either True or False: %s" % prepaid)
        else:
            prepaid = limit is not None
        return limit, prepaid

    def _PE_debitbalance(self, line):
        valid_answers = ('Ok', 'Failed', 'Not prepaid')
        lines = line.splitlines()
        result = lines[0].strip().capitalize()
        if result not in valid_answers:
            log.error("Invalid reply from rating engine: `%s'" % lines[0].strip())
            log.warn("Rating engine possible failed query: %s" % self.__request)
            raise RatingEngineError('Invalid rating engine response')
        elif result == 'Failed':
            log.warn("Rating engine failed query: %s" % self.__request)
            raise RatingEngineError('Rating engine failed query')
        else:
            try:
                timelimit = int(lines[1].split('=', 1)[1].strip())
                totalcost = lines[2].strip()
            except:
                log.error("Invalid reply from rating engine for DebitBalance on lines 2, 3: `%s'" % ("', `".join(lines[1:3])))
                timelimit = None
                totalcost = 0
            return timelimit, totalcost


    def _send_next_request(self):
        if self.connected:
            self.__request = self._request_queue.popleft()
            self.sendLine(self.__request)
            self._set_timeout()
#            log.debug("Sent request to rating engine: %s" % self.__request) #DEBUG
        else:
            self.__request = None

    def _respond(self, result, success=True):
        if self.__request is not None:
            req = self.__request
            self.__request = None
            try:
                if success:
                    req.deferred.callback(result)
                else:
                    req.deferred.errback(failure.Failure(RatingEngineError(result)))
            except defer.AlreadyCalledError:
                log.debug("Request %s was already responded to" % str(req))
        if self._request_queue:
            self._send_next_request()

    def _set_timeout(self, timeout=None):
        if timeout is None:
            timeout = self.factory.timeout
        self.__timeout_call = reactor.callLater(timeout/1000.0, self.timeoutConnection)

    def send_request(self, request):
        if not request.reliable and not self.connected:
            request.deferred.errback(failure.Failure(RatingEngineError("Connection with the Rating Engine is down")))
            return request
        self._request_queue.append(request)
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
        if self.application.disconnecting:
            return
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)


class RatingEngine(object):
    def __init__(self, address):
        self.address = address
        self.disconnecting = False
        self.connector = reactor.connectTCP(self.address[0], self.address[1], factory=RatingEngineFactory(self))
        self.connection = None
        self.__unsent_req = deque()

    def shutdown(self):
        self.disconnecting = True
        self.connector.disconnect()

    def connectionMade(self, connector):
        self.connection = connector.transport
        self.connection.protocol._request_queue.extend(self.__unsent_req)
        for req in self.__unsent_req:
            log.debug("Requeueing request for the rating engine: %s" % (req,))
        self.__unsent_req.clear()

    def connectionLost(self, connector, reason, protocol):
        while protocol._request_queue:
            req = protocol._request_queue.pop()
            if not req.reliable:
                log.debug("Request is considered failed: %s" % (req,))
                req.deferred.errback(failure.Failure(RatingEngineError("Connection with the Rating Engine is down")))
            else:
                log.debug("Saving request to be requeued later: %s" % (req,))
                self.__unsent_req.appendleft(req)
        self.connection = None
    
    def getCallLimit(self, call, max_duration=CallControlConfig.prepaid_limit, reliable=True):
        max_duration = max_duration or CallControlConfig.limit or 36000
        args = {}
        if call.inprogress:
            args['State'] = 'Connected'
        req = RatingRequest('MaxSessionTime', reliable=reliable, CallId=call.callid, From=call.billingParty, To=call.ruri,
                          Gateway=call.sourceip, Duration=max_duration, **args)
        if self.connection is not None:
            return self.connection.protocol.send_request(req).deferred
        else:
            self.__unsent_req.append(req)
            return req.deferred
    
    def debitBalance(self, call, reliable=True):
        req = RatingRequest('DebitBalance', reliable=reliable, CallId=call.callid, From=call.billingParty, To=call.ruri,
                      Gateway=call.sourceip, Duration=call.duration)
        if self.connection is not None:
            return self.connection.protocol.send_request(req).deferred
        else:
            self.__unsent_req.append(req)
            return req.deferred


class RatingEngineConnections(object):
    __metaclass__ = Singleton

    def __init__(self):
        self.connections = [RatingEngine(engine) for engine in RatingConfig.address]
        self.user_connections = {}

    @staticmethod
    def getConnection(call=None):
        engines = RatingEngineConnections()
        conn = random.choice(engines.connections)
        if call is None:
            return conn
        return engines.user_connections.setdefault(call.billingParty, conn)

    def remove_user(self, user):
        try:
            del self.user_connections[user]
        except KeyError:
            pass

    def shutdown(self):
        for engine in self.connections:
            engine.shutdown()
