
"""Rating engine interface implementation."""

import random
from collections import deque

from application.configuration import ConfigSection, ConfigSetting
from application.configuration.datatypes import EndpointAddress
from application import log
from application.python.types import Singleton

from twisted.internet.protocol import ReconnectingClientFactory
from twisted.internet.error import TimeoutError
from twisted.internet import reactor, defer, protocol
#from twisted.protocols.basic import LineOnlyReceiver
from twisted.python import failure

from callcontrol import configuration_file

##
## Rating engine configuration
##


class ThorNodeConfig(ConfigSection):
    __cfgfile__ = configuration_file
    __section__ = 'ThorNetwork'

    enabled = False


class RatingConfig(ConfigSection):
    __cfgfile__ = configuration_file
    __section__ = 'CDRTool'

    timeout = 500


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


class CallControlConfig(ConfigSection):
    __cfgfile__ = configuration_file
    __section__ = 'CallControl'

    prepaid_limit = ConfigSetting(type=TimeLimit, value=None)
    limit = ConfigSetting(type=TimeLimit, value=None)


class RatingError(Exception): pass
class RatingEngineError(RatingError): pass
class RatingEngineTimeoutError(TimeoutError): pass


class RatingRequest(bytes):
    def __new__(cls, command, reliable=True, **kwargs):
        reqstr = command + (kwargs and (' ' + ' '.join("%s=%s" % (name, value) for name, value in list(kwargs.items()))) or '')
        obj = super().__new__(cls, reqstr.encode('utf-8'))
        obj.command = command
        obj.reliable = reliable
        obj.kwargs = kwargs
        obj.deferred = defer.Deferred()
        return obj

    def __init__(self, *args, **kwargs):
        super(RatingRequest, self).__init__()


class RatingEngineProtocol(protocol.Protocol, TimeoutMixin):
    delimiter = b'\r\n'

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
        log.info("Connection to Rating Engine at %s:%d timed out" % (self.transport.getPeer().host, self.transport.getPeer().port))
        self.transport.loseConnection()

    def dataReceived(self, data):
        #log.debug('Rating response from %s:%S: %s' % (self.transport.getPeer().host, self.transport.getPeer().port, data))
        self.lineReceived(data.decode())

    def lineReceived(self, line):
        log.debug('Received response from rating engine %s:%s: %s' % (self.transport.getPeer().host, self.transport.getPeer().port, line.strip().replace("\n", " ")))
        if not line:
            return
        if self.__timeout_call is not None:
            self.__timeout_call.cancel()
        if self.__request is None:
            log.warning('Got reply for non-existing request: %s' % line)
            return
        try:
            self._respond(getattr(self, '_PE_%s' % self.__request.command.lower())(line))
        except AttributeError:
            self._respond("Unknown command in request. Cannot handle reply. Reply is: %s" % line, success=False)
        except Exception as e:
            self._respond(str(e), success=False)

    def _PE_maxsessiontime(self, line):
        lines = line.splitlines()

        try:
            limit = lines[0].strip().capitalize()
        except IndexError:
            raise ValueError("Empty reply from rating engine")
        try:
            limit = int(limit)
        except:
            if limit == 'None':
                limit = None
            elif limit == 'Locked':
                pass
            else:
                raise ValueError("rating engine limit must be a positive number, None or Locked: got '%s' from %s:%s" % (limit, self.transport.getPeer().host, self.transport.getPeer().port))
        else:
            if limit < 0:
                raise ValueError("rating engine limit must be a positive number, None or Locked: got '%s' from %s:%s" % (limit, self.transport.getPeer().host, self.transport.getPeer().port))

        data_list = [line.split('=', 1) for line in lines[1:]]
        info = {}
        for elem in data_list:
           try:
              info[elem[0]] = elem[1]
           except IndexError:
              pass

        if 'type' in info:
            type = info['type'].lower()
            if type == 'prepaid':
                prepaid = True
            elif type == 'postpaid':
                prepaid = False
            else:
                raise ValueError("prepaid must be either True or False: got '%s' from %s:%s" % (type, self.transport.getPeer().host, self.transport.getPeer().port))
        else:
            prepaid = limit is not None
        return limit, prepaid

    def _PE_debitbalance(self, line):
        valid_answers = ('Ok', 'Failed', 'Not prepaid')
        lines = line.splitlines()
        try:
            result = lines[0].strip().capitalize()
        except IndexError:
            raise ValueError("Empty reply from rating engine %s:%s", (self.transport.getPeer().host, self.transport.getPeer().port))
        if result not in valid_answers:
            log.error("Invalid reply from rating engine: got '%s' from %s:%s" % (lines[0].strip(), self.transport.getPeer().host, self.transport.getPeer().port))
            log.warning('Rating engine possible failed query: %s', self.__request)
            raise RatingEngineError('Invalid rating engine response')
        elif result == 'Failed':
            log.warning('Rating engine failed query: %s', self.__request)
            raise RatingEngineError('Rating engine failed query')
        else:
            try:
                timelimit = int(lines[1].split('=', 1)[1].strip())
                totalcost = lines[2].strip()
            except:
                log.error("Invalid reply from rating engine for DebitBalance on lines 2, 3: got '%s' from %s:%s" % ("', `".join(lines[1:3]), self.transport.getPeer().host, self.transport.getPeer().port))
                timelimit = None
                totalcost = 0
            return timelimit, totalcost

    def _send_next_request(self):
        if self.connected:
            self.__request = self._request_queue.popleft()
            self.delimiter = b'\r\n'
            log.debug("Send request to rating engine %s:%s: %s" % (self.transport.getPeer().host, self.transport.getPeer().port, self.__request.decode()))
            self.transport.write(self.__request)
            #self._set_timeout()
            self._set_timeout(self.factory.timeout)
            log.debug('Sent request to rating engine: %s', self.__request.decode())
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
                log.debug('Request %s was already responded to', req)
        if self._request_queue:
            self._send_next_request()

    def _set_timeout(self, timeout=None):
        if timeout is None:
            timeout = self.factory.timeout
#        self.__timeout_call = reactor.callLater(timeout/1000, self.timeoutConnection)

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


class DummyRatingEngine(object):
    def getCallLimit(self, call, max_duration=CallControlConfig.prepaid_limit, reliable=True):
        return defer.fail(failure.Failure(RatingEngineError("Connection with the Rating Engine not yet established")))

    def debitBalance(self, call, reliable=True):
        return defer.fail(failure.Failure(RatingEngineError("Connection with the Rating Engine not yet established")))


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
            log.debug('Re-queueing request for the rating engine: %s', req)
        self.__unsent_req.clear()

    def connectionLost(self, connector, reason, protocol):
        while protocol._request_queue:
            req = protocol._request_queue.pop()
            if not req.reliable:
                log.debug('Request is considered failed: %s', req)
                req.deferred.errback(failure.Failure(RatingEngineError("Connection with the Rating Engine is down")))
            else:
                log.debug('Saving request to be requeued later: %s', req)
                self.__unsent_req.appendleft(req)
        self.connection = None

    def getCallLimit(self, call, max_duration=CallControlConfig.prepaid_limit, reliable=True):
        max_duration = max_duration or CallControlConfig.limit or 36000
        args = {}
        if call.inprogress:
            args['State'] = 'Connected'
        req = RatingRequest('MaxSessionTime', reliable=reliable, CallId=call.callid, From=call.billingParty, To=call.ruri,
                            Gateway=call.sourceip, Duration=max_duration,
                            Application=call.sip_application, **args)
        if self.connection is not None:
            return self.connection.protocol.send_request(req).deferred
        else:
            self.__unsent_req.append(req)
            return req.deferred

    def debitBalance(self, call, reliable=True):
        req = RatingRequest('DebitBalance', reliable=reliable, CallId=call.callid, From=call.billingParty, To=call.ruri,
                      Gateway=call.sourceip, Duration=call.duration,
                      Application=call.sip_application)
        if self.connection is not None:
            return self.connection.protocol.send_request(req).deferred
        else:
            self.__unsent_req.append(req)
            return req.deferred


class RatingEngineAddress(EndpointAddress):
    default_port = 9024
    name = 'rating engine address'


class RatingEngineConnections(object):
    __metaclass__ = Singleton
    def __init__(self):
        self.user_connections = {}
        if not ThorNodeConfig.enabled:
            from callcontrol.rating.backends.opensips import OpensipsBackend
            self.backend = OpensipsBackend()
        else:
            from callcontrol.rating.backends.sipthor import SipthorBackend
            self.backend = SipthorBackend()

    @staticmethod
    def getConnection(call=None):
        engines = RatingEngineConnections()
        try:
            conn = random.choice(engines.backend.connections)
        except IndexError:
            return DummyRatingEngine()

        if call is None:
            return conn
        return engines.user_connections.setdefault(call.billingParty, conn)

    def remove_user(self, user):
        try:
            del self.user_connections[user]
        except KeyError:
            pass

    def shutdown(self):
        return defer.maybeDeferred(self.backend.shutdown)

