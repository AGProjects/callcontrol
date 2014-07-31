# Copyright (C) 2005-2010 AG Projects. See LICENSE for details.
#

"""
Implementation of Call objects used to store call information and manage
a call.
"""

import time
import re

from application import log

from twisted.internet.error import AlreadyCalled
from twisted.internet import reactor, defer

from callcontrol.rating import RatingEngineConnections
from callcontrol.opensips import DialogID, ManagementInterface


class CallError(Exception): pass


##
## Call data types
##

class ReactorTimer(object):
    def __init__(self, delay, function, args=[], kwargs={}):
        self.calldelay = delay
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.dcall = None

    def start(self):
        if self.dcall is None:
            self.dcall = reactor.callLater(self.calldelay, self.function, *self.args, **self.kwargs)

    def cancel(self):
        if self.dcall is not None:
            try:
                self.dcall.cancel()
            except AlreadyCalled:
                self.dcall = None

    def delay(self, seconds):
        if self.dcall is not None:
            try:
                self.dcall.delay(seconds)
            except AlreadyCalled:
                self.dcall = None

    def reset(self, seconds):
        if self.dcall is not None:
            try:
                self.dcall.reset(seconds)
            except AlreadyCalled:
                self.dcall = None


class Structure(dict):
    def __init__(self):
        dict.__init__(self)
    def __getitem__(self, key):
        elements = key.split('.')
        obj = self ## start with ourselves
        for e in elements:
            if not isinstance(obj, dict):
                raise TypeError("unsubscriptable object")
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
            raise AttributeError("'%s' object has no attribute '%s'" % (self.__class__.__name__, name))
        else:
            dict.__delitem__(self, name)
    def update(self, other):
        dict.update(self, other)
        for key, value in other.items():
            self.__dict__[key] = value


class Call(Structure):
    """Defines a call"""
    def __init__(self, request, application):
        Structure.__init__(self)
        self.prepaid   = request.prepaid
        self.locked    = False ## if the account is locked because another call is in progress
        self.expired   = False ## if call did consume its timelimit before being terminated
        self.created   = time.time()
        self.timer     = None
        self.starttime = None
        self.endtime   = None
        self.timelimit = None
        self.duration  = 0
        self.callid    = request.callid
        self.dialogid  = None
        self.diverter  = request.diverter
        self.ruri      = request.ruri
        self.sourceip  = request.sourceip
        self.token     = request.call_token
        self.sip_application = request.application # application is used below
        self['from']   = request.from_ ## from is a python keyword
        ## Determine who will pay for the call
        if self.diverter is not None:
            self.billingParty = 'sip:%s' % self.diverter
            self.user = self.diverter
        else:
            match = re.search(r'(?P<address>sip:(?P<user>([^@]+@)?[^\s:;>]+))', request.from_)
            if match is not None:
                self.billingParty = match.groupdict()['address']
                self.user = match.groupdict()['user']
            else:
                self.billingParty = None
                self.user = None
        self.__initialized = False
        self.application = application

    def __str__(self):
        return ("callid=%(callid)s from=%(from)s ruri=%(ruri)s "
                "diverter=%(diverter)s sourceip=%(sourceip)s "
                "timelimit=%(timelimit)s status=%%s" % self % self.status)

    def __expire(self):
        self.expired = True
        self.application.clean_call(self.callid)
        self.end(reason='call control', sendbye=True)

    def setup(self, request):
        """
        Perform call setup when first called (determine time limit and add timer).

        If call was previously setup but did not start yet, and the new request
        changes call parameters (ruri, diverter, ...), then update the call
        parameters and redo the setup to update the timer and time limit.
        """
        deferred = defer.Deferred()
        rating = RatingEngineConnections.getConnection(self)
        if not self.__initialized: ## setup called for the first time
            rating.getCallLimit(self, reliable=False).addCallbacks(callback=self._setup_finish_calllimit, errback=self._setup_error, callbackArgs=[deferred], errbackArgs=[deferred])
            return deferred
        elif self.__initialized and self.starttime is None:
            if self.diverter != request.diverter or self.ruri != request.ruri:
                ## call parameters have changed.
                ## unlock previous rating request
                self.prepaid = request.prepaid
                if self.prepaid and not self.locked:
                    rating.debitBalance(self).addCallbacks(callback=self._setup_finish_debitbalance, errback=self._setup_error, callbackArgs=[request, deferred], errbackArgs=[deferred])
                else:
                    rating.getCallLimit(self, reliable=False).addCallbacks(callback=self._setup_finish_calllimit, errback=self._setup_error, callbackArgs=[deferred], errbackArgs=[deferred])
                return deferred
        deferred.callback(None)
        return deferred

    def _setup_finish_calllimit(self, (limit, prepaid), deferred):
        if limit == 'Locked':
            self.timelimit = 0
            self.locked = True
        elif limit is not None:
            self.timelimit = limit
        else:
            from callcontrol.controller import CallControlConfig
            self.timelimit = CallControlConfig.limit
        if self.prepaid and not prepaid:
            self.timelimit = 0
            deferred.errback(CallError("Caller %s is regarded as postpaid by the rating engine and prepaid by OpenSIPS" % self.user))
            return
        else:
            self.prepaid = prepaid and limit is not None
        if self.timelimit is not None and self.timelimit > 0:
            self._setup_timer()
        self.__initialized = True
        deferred.callback(None)

    def _setup_finish_debitbalance(self, value, request, deferred):
        ## update call paramaters
        self.diverter = request.diverter
        self.ruri     = request.ruri
        if self.diverter is not None:
            self.billingParty = 'sip:%s' % self.diverter
        ## update time limit and timer
        rating = RatingEngineConnections.getConnection(self)
        rating.getCallLimit(self, reliable=False).addCallbacks(callback=self._setup_finish_calllimit, errback=self._setup_error, callbackArgs=[deferred], errbackArgs=[deferred])

    def _setup_timer(self, timeout=None):
        if timeout is None:
            timeout = self.timelimit
        self.timer = ReactorTimer(timeout, self.__expire)

    def _setup_error(self, fail, deferred):
        deferred.errback(fail)

    def start(self, request):
        assert self.__initialized, "Trying to start an unitialized call"
        if self.starttime is None:
            self.dialogid = DialogID(request.dialogid)
            self.starttime = time.time()
            if self.timer is not None:
                log.info("Call id %s of %s to %s started for maximum %d seconds" % (self.callid, self.user, self.ruri, self.timelimit))
                self.timer.start()
            # also reset all calls of user to this call's timelimit
            # no reason to alter other calls if this call is not prepaid
            if self.prepaid:
                rating = RatingEngineConnections.getConnection(self)
                rating.getCallLimit(self).addCallbacks(callback=self._start_finish_calllimit, errback=self._start_error)
                for callid in self.application.users[self.billingParty]:
                    if callid == self.callid:
                        continue
                    call = self.application.calls[callid]
                    if not call.prepaid:
                        continue # only alter prepaid calls
                    if call.inprogress:
                        call.timelimit = self.starttime - call.starttime + self.timelimit
                        if call.timer:
                            call.timer.reset(self.timelimit)
                            log.info("Call id %s of %s to %s also set to %d seconds" % (callid, call.user, call.ruri, self.timelimit))
                    elif not call.complete:
                        call.timelimit = self.timelimit
                        call._setup_timer()

    def _start_finish_calllimit(self, (limit, prepaid)):
        if limit not in (None, 'Locked'):
            delay = limit - self.timelimit
            for callid in self.application.users[self.billingParty]:
                call = self.application.calls[callid]
                if not call.prepaid:
                    continue # only alter prepaid calls
                if call.inprogress:
                    call.timelimit += delay
                    if call.timer:
                        call.timer.delay(delay)
                        log.info("Call id %s of %s to %s %s maximum %d seconds" % (callid, call.user, call.ruri, (call is self) and 'connected for' or 'previously connected set to', limit))
                elif not call.complete:
                    call.timelimit = self.timelimit
                    call._setup_timer()

    def _start_error(self, fail):
        log.info("Could not get call limit for call id %s of %s to %s" % (self.callid, self.user, self.ruri))

    def end(self, calltime=None, reason=None, sendbye=False):
        if sendbye and self.dialogid is not None:
            ManagementInterface().end_dialog(self.dialogid)
        if self.timer:
            self.timer.cancel()
            self.timer = None
        fullreason = '%s%s' % (self.inprogress and 'disconnected' or 'canceled', reason and (' by %s' % reason) or '')
        if self.inprogress:
            self.endtime = time.time()
            duration = self.endtime - self.starttime
            if calltime:
                ## call did timeout and was ended by external means (like mediaproxy).
                ## we were notified of this and we have the actual call duration in `calltime'
                #self.endtime = self.starttime + calltime
                self.duration = calltime
                log.info("Call id %s of %s to %s was already disconnected (ended or did timeout) after %s seconds" % (self.callid, self.user, self.ruri, self.duration))
            elif self.expired:
                self.duration = self.timelimit
                if duration > self.timelimit + 10:
                    log.warn("Time difference between sending BYEs and actual closing is > 10 seconds")
            else:
                self.duration = duration
        if self.prepaid and not self.locked and self.timelimit > 0:
            ## even if call was not started we debit 0 seconds anyway to unlock the account
            rating = RatingEngineConnections.getConnection(self)
            rating.debitBalance(self).addCallbacks(callback=self._end_finish, errback=self._end_error, callbackArgs=[reason and fullreason or None])
        elif reason is not None:
            log.info("Call id %s of %s to %s %s%s" % (self.callid, self.user, self.ruri, fullreason, self.duration and (' after %d seconds' % self.duration) or ''))

    def _end_finish(self, (timelimit, value), reason):
        if timelimit is not None and timelimit > 0:
            now = time.time()
            for callid in self.application.users.get(self.billingParty, ()):
                call = self.application.calls[callid]
                if not call.prepaid:
                    continue # only alter prepaid calls
                if call.inprogress:
                    call.timelimit = now - call.starttime + timelimit
                    if call.timer:
                        log.info("Call id %s of %s to %s previously connected set to %d seconds" % (callid, call.user, call.ruri, timelimit))
                        call.timer.reset(timelimit)
                elif not call.complete:
                    call.timelimit = timelimit
                    call._setup_timer()
        # log ended call
        if self.duration > 0:
            log.info("Call id %s of %s to %s %s after %d seconds, call price is %s" % (self.callid, self.user, self.ruri, reason, self.duration, value))
        elif reason is not None:
            log.info("Call id %s of %s to %s %s" % (self.callid, self.user, self.ruri, reason))

    def _end_error(self, fail):
        log.info("Could not debit balance for call id %s of %s to %s" % (self.callid, self.user, self.ruri))

    status     = property(lambda self: self.inprogress and 'in-progress' or 'pending')
    complete   = property(lambda self: self.dialogid is not None)
    inprogress = property(lambda self: self.starttime is not None and self.endtime is None)

#
# End Call data types
#
