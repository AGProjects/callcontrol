# Copyright (C) 2005-2008 AG Projects.
#

"""Implementation of a call control server for SER."""

import os
import grp
import re
import cPickle
import time

from application.configuration import ConfigSection, ConfigFile
from application.python.queue import EventQueue
from application.process import process
from application import log

from twisted.internet.protocol import Factory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.internet import reactor, defer
from twisted.python import failure

from callcontrol.scheduler import RecurrentCall, KeepRunning
from callcontrol.raddb import RadiusDatabase, RadiusDatabaseError
from callcontrol.sip import Call, SipClient
from callcontrol.rating import RatingEngineConnections
from callcontrol import configuration_filename, backup_calls_file


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
    _datatypes = {'limit': TimeLimit}
    socket        = "%s/socket" % process.runtime_directory
    group         = 'openser'
    limit         = None
    timeout       = 24*60*60 ## timeout calls that are stale for more than 24 hours.
    setupTime     = 90       ## timeout calls that take more than 1'30" to setup.
    checkInterval = 60       ## check for staled calls and calls that did timeout at every minute.

config_file = ConfigFile(configuration_filename)
config_file.read_settings('CallControl', CallControlConfig)



## Classes

class CommandError(Exception):        pass
class CallControlError(Exception):    pass
class NoProviderError(Exception):     pass
class InvalidRequestError(Exception): pass


class CallsMonitor(object):
    """Check for staled calls and calls that did timeout and were closed by external means"""
    def __init__(self, period, application):
        self.application = application
        self.reccall = RecurrentCall(period, self.run)

    def run(self):
        ## Find out terminated calls
        deferred1 = self.application.db.getTerminatedCalls(self.application.calls)
        deferred1.addCallbacks(callback=self._clean_calls, errback=self._err_handle, callbackArgs=[self._handle_terminated])
        deferred2 = self.application.db.getTimedoutCalls(self.application.calls)
        deferred2.addCallbacks(callback=self._clean_calls, errback=self._err_handle, callbackArgs=[self._handle_timedout])
        defer.DeferredList([deferred1, deferred2]).addCallback(self._finish_checks)
        return KeepRunning

    def shutdown(self):
        self.reccall.cancel()

    def _clean_calls(self, calls, clean_function):
        for callid, callinfo in calls.items():
            call = self.application.calls.get(callid)
            if call:
                self.application.clean_call(callid)
                clean_function(call, callinfo)

    def _err_handle(self, fail):
        log.error("Couldn't query database for terminated/timedout calls: %s" % fail.value)

    def _handle_terminated(self, call, callinfo):
        call.end(calltime=callinfo['duration'], reason='calls monitor as terminated')

    def _handle_timedout(self, call, callinfo):
        sip = SipClient()
        sip.send(call.callerBye)
        sip.send(call.calledBye)
        call.end(reason='calls monitor as timedout')

    def _finish_checks(self, value):
        ## Also do the rest of the checking
        now = time.time()
        staled = []
        nosetup = []
        for callid, call in self.application.calls.items():
            if not call.complete and (now - call.created >= CallControlConfig.setupTime):
                self.application.clean_call(callid)
#                log.info("Call id %s of %s didn\'t setup in %d seconds" % (callid, call.user, CallControlConfig.setupTime))
                nosetup.append(call)
            elif call.inprogress and call.timer is not None:
                continue ## this call will be expired by its own timer
            elif now - call.created >= CallControlConfig.timeout:
                self.application.clean_call(callid)
#                log.info("Call id %s of %s was stale", (callid, call.user))
                staled.append(call)
        ## Terminate staled
        for call in staled:
            call.end(reason='calls monitor as staled')
        ## Terminate calls that didn't setup in setupTime
        for call in nosetup:
            call.end(reason="calls monitor as it didn't setup in %d seconds" % CallControlConfig.setupTime)


class CallControlProtocol(LineOnlyReceiver):
    def lineReceived(self, line):
        if not line:
            return
        try:
            req = Request(line)
        except InvalidRequestError, e:
            self._send_error_reply(failure.Failure(e))
        else:
#            log.debug("Got request: %s" % str(req)) #DEBUG
            def _unknown_handler(req):
                req.deferred.errback(failure.Failure(CommandError(req)))
            try:
                getattr(self, '_CC_%s' % req.cmd, _unknown_handler)(req)
            except Exception, e:
                self._send_error_reply(failure.Failure(e))
            else:
                req.deferred.addCallbacks(callback=self._send_reply, errback=self._send_error_reply)

    def _send_reply(self, msg):
#        log.debug('Sent reply: %s' % msg) #DEBUG
        self.sendLine(msg)

    def _send_error_reply(self, fail):
        log.error(fail.value)
        self.sendLine('Error')

    def _CC_init(self, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            call = Call(req, self.factory.application)
            if call.provider is None:
                req.deferred.callback('No provider')
                return
#            log.debug("Call id %s added to list of controlled calls" % (call.callid)) #DEBUG
            self.factory.application.calls[req.callid] = call
        deferred = call.setup(req)
        deferred.addCallbacks(callback=self._CC_finish_init, errback=self._send_error_reply, callbackArgs=[req])

    def _CC_finish_init(self, value, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            log.error("Call id %s disappeared before we could finish initializing it" % req.callid)
            req.deferred.callback('Error')
        else:
            if call.locked: ## prepaid account already locked by another call
                log.info("Call id %s of %s forbidden because the account is locked" % (req.callid, call.user))
                self.factory.application.clean_call(req.callid)
                call.end()
                req.deferred.callback('Locked')
            elif call.timelimit == 0: ## prepaid account with no credit
                log.info("Call id %s of %s forbidden because credit is too low" % (req.callid, call.user))
                self.factory.application.clean_call(req.callid)
                call.end()
                req.deferred.callback('No credit')
            elif call.timelimit is None: ## no limit for call
                log.info("Call id %s of %s is not limited" % (req.callid, call.user))
                self.factory.application.clean_call(req.callid)
                call.end()
                req.deferred.callback('Ok')
            else:
                req.deferred.callback('Ok')

    def _CC_start(self, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            req.deferred.callback('Not found')
        else:
            call.start(req)
            req.deferred.callback('Ok')

    def _CC_update(self, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            req.deferred.callback('Not found')
        else:
            call.update(req)
            req.deferred.callback('Ok')

    def _CC_stop(self, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            req.deferred.callback('Not found')
        else:
            self.factory.application.clean_call(req.callid)
            call.end(reason='user')
            req.deferred.callback('Ok')

    def _CC_debug(self, req):
        log.debug(str(self.factory.application.calls))


class CallControlFactory(Factory):
    protocol = CallControlProtocol

    def __init__(self, application):
        self.application = application


class CallControlServer(object):
    def __init__(self, path=None, group=None):
        self.path = path or CallControlConfig.socket
        self.group = group or CallControlConfig.group
        try:
            os.unlink(self.path)
        except OSError:
            pass
        
        self.listening = None
        self.sipclient = None
        self.engines = None
        self.monitor = None
        self.db = RadiusDatabase()
        
        self.calls = {}
        self._restore_calls()

    def clean_call(self, callid):
        try:
            del self.calls[callid]
#            log.debug("Call id %s removed from the list of controlled calls" % callid) #DEBUG
        except KeyError:
            pass

    def run(self):
        ## Do the startup stuff
        self.on_startup()
        ## And start reactor
        reactor.run()
        ## And do the shutdown
        self.on_shutdown()

    def stop(self):
        reactor.stop()

    def on_startup(self):
        ## First set up listening on the unix socket
        try:
            gid = grp.getgrnam(self.group)[2]
            mode = 0660
        except KeyError, IndexError:
            gid = -1
            mode = 0666
        self.listening = reactor.listenUNIX(address=self.path, factory=CallControlFactory(self), mode=mode)
        ## Make it writable only to the SIP proxy group members
        try:
            os.chown(self.path, -1, gid)
        except OSError:
            log.warn("Couldn't set access rights for %s" % self.path)
            log.warn("SER may not be able to communicate with us!")

        ## Then setup the CallsMonitor
        self.monitor = CallsMonitor(CallControlConfig.checkInterval, self)
        ## Initialize SipClient
        self.sipclient = SipClient()
        ## Open the connection to the rating engines
        self.engines = RatingEngineConnections()

    def on_shutdown(self):
        if self.listening is not None:
            self.listening.stopListening()
        if self.sipclient is not None:
            self.sipclient.shutdown()
        if self.engines is not None:
            self.engines.shutdown()
        if self.monitor is not None:
            self.monitor.shutdown()
        self.db.close()
        self._save_calls()
    
    def _save_calls(self):
        if self.calls:
            log.info('Saving calls')
            calls_file = '%s/%s' % (process.runtime_directory, backup_calls_file)
            try:
                f = open(calls_file, 'w')
            except:
                pass
            else:
                for call in self.calls.values():
                    call.application = None
                    ## we will mark timers with 'running' or 'idle', depending on their current state,
                    ## to be able to correctly restore them later (Timer objects cannot be pickled)
                    if call.timer is not None:
                        if call.inprogress:
                            call.timer.cancel()
                            call.timer = 'running' ## temporary mark that this timer was running
                        else:
                            call.timer = 'idle'    ## temporary mark that this timer was not running
                failed_dump = False
                try:
                    try:
                        cPickle.dump(self.calls, f)
                    except Exception, why:
                        log.warn("Failed to dump call list: %s" % why)
                        failed_dump = True
                finally:
                    f.close()
                if failed_dump:
                    try:    os.unlink(calls_file)
                    except: pass
                else:
                    log.info("Saved calls: %s" % str(self.calls.keys()))
            self.calls = {}

    def _restore_calls(self):
        calls_file = '%s/%s' % (process.runtime_directory, backup_calls_file)
        try:
            f = open(calls_file, 'r')
        except:
            pass
        else:
            try:
                self.calls = cPickle.load(f)
            except Exception, why:
                log.warn("Failed to load calls saved in the previous session: %s" % why)
            f.close()
            try:    os.unlink(calls_file)
            except: pass
            if self.calls:
                log.info("Restoring calls saved previously: %s" % str(self.calls.keys()))
                ## the calls in the 2 sets below are never overlapping because closed and terminated
                ## calls have different database fingerprints. so the dictionary update below is safe
                try:
                    terminated = self.db.query(RadiusDatabase.RadiusTask(None, 'terminated', calls=self.calls))   ## calls terminated by caller/called
                    didtimeout = self.db.query(RadiusDatabase.RadiusTask(None, 'timedout', calls=self.calls))     ## calls closed by mediaproxy after a media timeout
                except RadiusDatabaseError, e:
                    log.error("Could not query database: %s" % e)
                else:
                    for callid, call in self.calls.items():
                        callinfo = terminated.get(callid) or didtimeout.get(callid)
                        if callinfo:
                            ## call already terminated or did timeout in mediaproxy
                            del self.calls[callid]
                            callinfo['call'] = call
                            call.timer = None
                            continue
                    ## close all calls that were already terminated or did timeout 
                    count = 0
                    sip = SipClient()
                    for callinfo in terminated.values():
                        call = callinfo.get('call')
                        if call is not None:
                            call.end(calltime=callinfo['duration'])
                            count += 1
                    for callinfo in didtimeout.values():
                        call = callinfo.get('call')
                        if call is not None:
                            sip.send(call.callerBye)
                            sip.send(call.calledBye)
                            call.end()
                            count += 1
                    if count > 0:
                        log.info("Removed %d already terminated call%s" % (count, 's'*(count!=1)))
                for callid, call in self.calls.items():
                    call.application = self
                    if call.timer == 'running':
                        now = time.time()
                        remain = call.starttime + call.timelimit - now
                        if remain < 0:
                            call.timelimit = int(round(now - call.starttime))
                            remain = 0
                        call._setup_timer(remain)
                        call.timer.start()
                    elif call.timer == 'idle':
                        call._setup_timer()

class Request(object):
    """A request parsed into a structure based on request type"""
    __methods = {'init':   ('callid', 'contact', 'cseq', 'diverter', 'ruri', 'sourceip', 'from', 'fromtag', 'to'),
                 'start':  ('callid', 'contact', 'totag'),
                 'update': ('callid', 'cseq', 'fromtag'),
                 'stop':   ('callid',),
                 'debug':  ()}
    def __init__(self, message):
        try:    message + ''
        except: raise ValueError, "message should be a string"
        lines = [line.strip() for line in message.splitlines() if line.strip()]
        if not lines:
            raise InvalidRequestError, 'missing input'
        cmd = lines[0].lower()
        if cmd not in self.__methods.keys():
            raise InvalidRequestError, "unknown request: %s" % cmd
        try:
            parameters = dict([re.split(r':\s+', l, 1) for l in lines[1:]])
        except ValueError:
            raise InvalidRequestError, "badly formatted request"
        for p in self.__methods[cmd]:
            try: 
                parameters[p]
            except KeyError:
                raise InvalidRequestError, "missing %s from request" % p
        self.cmd = cmd
        self.deferred = defer.Deferred()
        self.__dict__.update(parameters)
        try:
            getattr(self, '_RE_%s' % self.cmd)()
        except AttributeError:
            pass

    def _RE_init(self):
        self.from_ = self.__dict__['from']
        if self.cmd=='init' and self.diverter.lower()=='none':
            self.diverter = None

    def __str__(self):
        if self.cmd == 'init':
            return "%(cmd)s: callid=%(callid)s from=%(from_)s to=%(to)s ruri=%(ruri)s cseq=%(cseq)s diverter=%(diverter)s sourceip=%(sourceip)s contact=%(contact)s fromtag=%(fromtag)s" % self.__dict__
        elif self.cmd == 'start':
            return "%(cmd)s: callid=%(callid)s contact=%(contact)s totag=%(totag)s" % self.__dict__
        elif self.cmd == 'update':
            return "%(cmd)s: callid=%(callid)s cseq=%(cseq)s fromtag=%(fromtag)s" % self.__dict__
        elif self.cmd == 'stop':
            return "%(cmd)s: callid=%(callid)s" % self.__dict__
        elif self.cmd == 'debug':
            return "%(cmd)s" % self.__dict__
        else:
            return object.__str__(self)

