# Copyright (C) 2005-2008 AG Projects.
#

"""
 Implementation of a SIP Null client

 The SIP Null client will send fake BYE messages and will receive their replies,
but will ignore them completely.
"""

import time
import random

from application.configuration import ConfigSection, ConfigFie
from application.configuration.datatypes import NetworkAddress, EndpointAddress
from application.python.util import Singleton
from application import log

from twisted.internet.protocol import DatagramProtocol
from twisted.internet import reactor

from callcontrol import configuration_filename

##
## SIP configuration
##
class SipProxyAddress(EndpointAddress):
    _defaultPort = 5060
    _name = 'SIP proxy address'

class SipConfig(ConfigSection):
    _dataTypes = {'listen': NetworkAddress, 'proxy': SipProxyAddress}
    listen     = ('0.0.0.0', 5070)
    proxy      = None

## We use this to overwrite some of the settings above on a local basis if needed
config_file = ConfigFile(configuration_filename)
config_file.read_settings('SIP', SipConfig)

# check these. what should be enforced by the data type?
if SipConfig.listen is None:
    log.fatal("listening address for the SIP client is not defined")
if SipConfig.proxy is None:
    log.fatal("SIP proxy address is not defined")

## Determine what is the address we will send from, based on configuration
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.bind(SipConfig.listen)
except socket.error, why:
    fatal("Cannot bind to %s:%d for SIP messaging: %s" % tuple(SipConfig.listen + (why[1],)))
s.connect(SipConfig.proxy)
SipConfig._sending_address = s.getsockname()
s.close()
del s

#
# End SIP configuration
#

##
## SIP client implementation
##

class SipError(Exception): pass
class SipClientError(SipError): pass
class SipTransmisionError(SipError): pass


class SipNullClientProtocol(DatagramProtocol):
    def datagramReceived(self, data, (host, port)):
        pass ## ignore reply


class SipClient(object):
    """
    A dumb SIP client, that is able to send a SIP request and wait for the
    reply, which it'll ignore. The SIP request must be build by the caller.
    Returns a singleton instance.
    """
    __metaclass__ = Singleton
    def __init__(self, listen=None, proxy=None):
        self.listen = listen or SipConfig.listen
        self.proxy = proxy or SipConfig.proxy
        self.protocol = SipNullClientProtocol()
        reactor.listenUDP(self.listen, self.protocol)

    def send(self, data):
        self.protocol.transport.write(data, self.proxy)

#
# End SIP client implementation
#

##
## Call data types
##

class SipClientInfo(Structure):
    """Describes the SIP client/proxy/connection parameters"""
    def __init__(self):
        Structure.__init__(self)
        self.name    = 'Call Controller'
        self.address = '%s:%d' % SipConfig._sending_address
        self.proxy   = '%s:%d' % SipConfig.proxy

sipClientInfo = SipClientInfo()


class InvalidRequestError(Exception): pass


class ReactorTimer(object):
    def __init__(self, delay, function, args=[], kwargs={}):
        self.delay = delay
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.dcall = None

    def start(self):
        if self.dcall is None:
            self.dcall = reactor.callLater(self.delay, self.function, *args, **kwargs)

    def cancel(self):
        if self.dcall is not None:
            self.dcall.cancel()

class Structure(dict):
    def __init__(self):
        dict.__init__(self)
    def __getitem__(self, key):
        elements = key.split('.')
        obj = self ## start with ourselves
        for e in elements:
            if not isinstance(obj, dict):
                raise TypeError, "unsubscriptable object"
            obj = dict.__getitem__(obj, e)
        return obj
    def __setitem__(self, key, value):
        self.__dict__[key] = value
        dict.__setitem__(self, key, value)
    def __delitem__(self, key):
        dict.__delitem__(self, key)
        del self.__dict__[key]
    __setattr__ = __setitem__
    def __delattr__(self, name):
        try:
            del self.__dict__[name]
        except KeyError:
            raise AttributeError, "'%s' object has no attribute '%s'" % (self.__class__.__name__, name)
        else:
            dict.__delitem__(self, name)
    def update(self, other):
        dict.update(self, other)
        for key, value in other.items():
            self.__dict__[key] = value

class Endpoint(Structure):
    """Parameters that belong to a given endpoint during a call"""
    def __init__(self, request):
        Structure.__init__(self)
        try:
            self.cseq = int(request.cseq)
        except:
            self.cseq = 1
        self.nextcseq = self.cseq + 1
        self.contact  = request.contact
        self.branch   = 'z9hG4bK' + str(random.choice(xrange(1000000, 9999999)))
    def update(self, request):
        try:
            self.cseq = int(request.cseq)
        except:
            self.cseq = 1
        self.nextcseq = self.cseq + 1
    #def gethp(self): return self.contact[self.contact.find('@')+1:]
    #hostport = property(gethp)
    #del gethp

class Call(Structure):
    """Defines a call"""
    def __init__(self, request):
        Structure.__init__(self)
        self.prepaid   = False
        self.locked    = False ## if the account is locked because another call is in progress
        self.expired   = False ## if call did consume its timelimit before being terminated
        self.created   = time.time()
        self.timer     = None
        self.starttime = None
        self.endtime   = None
        self.timelimit = None
        self.duration  = 0
        self.caller    = Endpoint(request)
        self.called    = None
        self.callid    = request.callid
        self.diverter  = request.diverter
        self.ruri      = request.ruri
        self.sourceip  = request.sourceip
        self.fromtag   = request.fromtag  
        self.to        = request.to
        self['from']   = request['from'] ## from is a python keyword
        self.totag     = None
        self.sipclient = sipClientInfo
        ## Determine who will pay for the call
        if self.diverter is not None:
            self.billingParty = 'sip:%s' % self.diverter
        else:
            match = re.search(r'(?P<address>sip:[^@]+@[^\s:;>]+)', request['from'])
            if match is not None:
                self.billingParty = match.groupdict()['address']
            else:
                self.billingParty = 'unknown'
        ## Determine which provider will handle the call
        match = re.search(r'sip:[^@]+@(?P<hostname>.*)', self.billingParty)
        if match is not None:
            self.provider = match.groupdict()['hostname']
        else:
            self.provider = None
        ## Extract the destination username
        match = re.search(r'sip:(?P<user>[^@\s]+)@.*', request.to)
        if match is not None:
            self.touser = match.groupdict()['user']
        else:
            self.touser = 'unknown'
        self.__initialized = False

    def __str__(self):
        return ("callid=%(callid)s from=%(from)s to=%(to)s ruri=%(ruri)s "
                "diverter=%(diverter)s sourceip=%(sourceip)s provider=%(provider)s "
                "timelimit=%(timelimit)s status=%%s" % self % self.status)
    
    def __expire(self):
        self.expired = True
        sip = SipClient()
        sip.send(self.callerBye)
        sip.send(self.calledBye)
        #time.sleep(0.001)
        #sip.send(self.callerBye)
        #sip.send(self.calledBye)
        self.end() ## we can end here, or wait for SER to call us with a stop command after it receives the BYEs

    def setup(self, request):
        """
        Perform call setup when first called (determine time limit and add timer).
        
        If call was previously setup but did not start yet, and the new request
        changes call parameters (ruri, diverter, ...), then update the call
        parameters and redo the setup to update the timer and time limit.
        """
        deferred = defer.Deferred()
        prepaid = PrepaidEngineConnection.getConnection(self.provider)
        if not self.__initialized: ## setup called for the first time
            prepaid.getCallLimit(self).addCallbacks(callback=self._setup_finish_calllimit, errback=self._setup_error, callbackArgs=[deferred])
            return deferred
        elif self.__initialized and self.starttime is None: ## call was previously setup but not yet started
            if self.diverter != request.diverter or self.ruri != request.ruri:
                ## call parameters have changed.
                ## unlock previous prepaid request
                if self.prepaid and not self.locked:
                    prepaid.debitBalance(self).addCallbacks(callback=self._setup_finish_debitbalance, errback=self._setup_error, callbackArgs=[deferred], errbackArgs=[deferred])
                    return deferred
        deferred.callback(None)
        return deferred

    def _setup_finish_calllimit(self, limit, deferred):
        if limit == 'Locked':
            self.timelimit = 0
            self.locked = True
        else:
            self.timelimit = limit
        if self.timelimit is None:
            self.timelimit = CallControlConfig.limit
            self.prepaid = False
        else:
            self.prepaid = True
        if self.timelimit is not None and self.timelimit > 0:
            self._setup_timer()
        self.__initialized = True
        deferred.callback(None)

    def _setup_finish_debitbalance(self, value, deferred):
        ## update call paramaters
        self.caller.update(request)
        self.diverter = request.diverter
        self.ruri     = request.ruri
        if self.diverter is not None:
            self.billingParty = 'sip:%s' % self.diverter
        ## update time limit and timer
        prepaid.getCallLimit(self).addCallbacks(callback=self._setup_finish_calllimit, errback=self._setup_error, callbackArgs=[deferred], errbackArgs=[deferred])

    def _setup_timer(self, timeout=self.timelimit):
        self.timer = ReactorTimer(self.timelimit, self.__expire)
        self.timer.setName('CallExpiringTimer')

    def _setup_error(self, fail, deferred):
        deferred.errback(fail)

    def start(self, request):
        assert self.__initialized, "trying to start an unitialized call"
        if self.starttime is None:
            self.called = Endpoint(request)
            self.totag  = request.totag
            self.starttime = time.time()
            if self.timer is not None:
                self.timer.start()

    def update(self, request):
        assert self.__initialized, "trying to update an unitialized call"
        if self.fromtag == request.fromtag:
            self.caller.update(request)
        elif self.totag == request.fromtag:
            self.called.update(request)
        else:
            log.warn("trying to update from nonexistent party (from tag mismatch)")

    def end(self, calltime=None):
        if self.timer:
            self.timer.cancel()
        if self.inprogress:
            self.endtime = time.time()
            duration = int(round(self.endtime - self.starttime))
            if calltime:
                ## call did timeout and was ended by external means (like mediaproxy).
                ## we were notified of this and we have the actual call duration in `calltime'
                #self.endtime = self.starttime + calltime
                self.duration = calltime
                log.info("closing call that was already terminated (ended or did timeout)")
            elif self.expired:
                self.duration = self.timelimit
                if duration > self.timelimit + 10:
                    log.warn("time difference between sending BYEs and actual closing is > 10 seconds")
            else:
                self.duration = duration
        if self.prepaid and not self.locked:
            ## even if call was not started we debit 0 seconds anyway to unlock the account
            prepaid = PrepaidEngineConnection.getConnection(self.provider)
            prepaid.debitBalance(self) # there is basically no result, so we ignore the deferred

    def getbye1(self):
        """Generate a BYE as if it came from the caller"""
        assert self.complete, 'Incomplete call'
        return ('BYE sip:%(called.contact)s SIP/2.0\r\n'
                'Via: SIP/2.0/UDP %(sipclient.address)s;branch=%(caller.branch)s\r\n'
                'From: %(from)s;tag=%(fromtag)s\r\n'
                'To: %(to)s;tag=%(totag)s\r\n'
                'Call-ID: %(callid)s\r\n'
                'CSeq: %(caller.nextcseq)s BYE\r\n'
                'User-Agent: %(sipclient.name)s\r\n'
                'Route: <sip:%(touser)s@%(sipclient.proxy)s;ftag=%(fromtag)s;lr=on>\r\n'
                'Content-Length: 0\r\n'
                '\r\n') % self
    def getbye2(self):
        """Generate a BYE as if it came from the called"""
        assert self.complete, 'Incomplete call'
        return ('BYE sip:%(caller.contact)s SIP/2.0\r\n'
                'Via: SIP/2.0/UDP %(sipclient.address)s;branch=%(called.branch)s\r\n'
                'From: %(to)s;tag=%(totag)s\r\n'
                'To: %(from)s;tag=%(fromtag)s\r\n'
                'Call-ID: %(callid)s\r\n'
                'CSeq: %(called.nextcseq)s BYE\r\n'
                'User-Agent: %(sipclient.name)s\r\n'
                'Route: <sip:%(touser)s@%(sipclient.proxy)s;ftag=%(fromtag)s;lr=on>\r\n'
                'Content-Length: 0\r\n'
                '\r\n') % self
    def getcp(self): return (None not in (self.called, self.totag))
    def getip(self): return (self.starttime is not None and self.endtime is None)
    def getst(self): return self.inprogress and 'in-progress' or 'pending'
    status     = property(getst)
    complete   = property(getcp)
    inprogress = property(getip)
    callerBye  = property(getbye1)
    calledBye  = property(getbye2)
    del getcp, getip, getst, getbye1, getbye2

#
# End Call data types
#
