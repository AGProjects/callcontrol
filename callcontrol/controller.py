# Copyright (C) 2005-2008 AG Projects.
#

"""Implementation of a call control server for SER."""

import os
import grp

from application.configuration import ConfigSection, ConfigFile
from application.python.queue import EventQueue
from application.process import process
from application import log

from twisted.internet.protocol import Factory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.internet import reactor, defer
from twisted.python import failure

from callcontrol.scheduler import RecurrentCall, KeepRunning
from callcontrol.cdrdb import CDRDatabase
from callcontrol.sip import Structure
from callcontrol import configuration_filename, calls_file


class TimeLimit(int):
    '''A positive time limit (in seconds) or None'''
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
    _dataTypes = {'limit': TimeLimit}
    socket        = '/var/run/callcontrol/socket'
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


class CallsMonitor(object):
    '''Check for staled calls and calls that did timeout and were closed by external means'''
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

    def shutdown(self):
        self.reccall.cancel()

    def _clean_calls(self, calls, clean_function):
        count = 0
        for callid, callinfo in calls.items():
            call = self.application.calls.get(callid)
            if call:
                del self.application.calls[callid]
                clean_function(call, callinfo)
                count += 1
        if count > 0:
            log.info("removed %d externally closed call%s" % (count, 's'*(count!=1)))

    def _err_handle(self, failure):
        log.error("Couldn't query database for terminated/timedout calls: %s" % failure.value)

    def _handle_terminated(self, call, callinfo):
        call.end(calltime=callinfo['duration'])

    def _handle_timedout(self, call, callinfo):
        sip = SipClient()
        sip.send(call.callerBye)
        sip.send(call.calledBye)
        call.end()

    def _finish_checks(self, value)
        ## Also do the rest of the checking
        staled = []
        nosetup = []
        for callid, call in self.application.calls.items():
            if not call.complete and (now - call.created >= CallControlConfig.setupTime):
                del self.application.calls[callid]
                nosetup.append(call)
            elif call.inprogress and call.timer is not None:
                continue ## this call will be expired by its own timer
            elif now - call.created >= CallControlConfig.stalePeriod:
                del self.application.calls[callid]
                staled.append(call)
        ## Terminate staled
        for call in staled:
            call.end()
        count = len(staled)
        if count > 0:
            log.info("expired %d staled call%s" % (count, 's'*(count!=1)))
        ## Terminate calls that didn't setup in setupTime
        for call in nosetup:
            call.end()
        count = len(nosetup)
        if count > 0:
            log.info("expired %d call%s that didn't setup in %d seconds" % (count, 's'*(count!=1), CallControlConfig.setupTime))


class CallControlProtocol(LineOnlyReceiver):
    def lineReceived(self, line):
        req = Request(line)
        def _unknown_handler(req):
            req.deferred.errback(failure.Failure(CommandError(req)))
        getattr(self, '_CC_%s' % req.cmd, _unknown_handler)(req)
        req.deferred.addCallbacks(callback=self._send_reply, errback=self._send_error_reply)

    def _send_reply(self, msg):
        self.sendLine(msg)

    def _send_error_reply(self, fail):
        log.error(fail.value)
        self.sendLine('Error')

    def _CC_init(self, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            call = Call(req)
            if call.provider is None:
                req.deferred.callback('No provider')
            self.factory.application.calls[req.callid] = call
        deferred = call.setup(req)
        deferred.addCallbacks(callback=self._CC_finish_init, errback=self._send_error_reply, callbackArgs=[req])

    def _CC_finish_init(self, call, req):
        if call.locked: ## prepaid account already locked by another call
            call.end()
            req.deferred.callback('Locked')
        elif call.timelimit == 0: ## prepaid account with no credit
            call.end()
            req.deferred.callback('No credit')
        else:
            req.deferred.callback('Ok')

    def _CC_start(self, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            req.deferred.callback('Not found')
            return
        call.start(req)
        req.deferred.callback('Ok')

    def _CC_update(self, req):
        try:
            call = self.factory.application.calls[req.callid]
        except KeyError:
            req.deferred.callback('Not found')
            return
        call.update(req)
        req.deferred.callback('Ok')

    def _CC_stop(self, req):
        try:
            call = self.factory.application.calls[req.callid]
            del self.factory.application.calls[req.callid]
        except KeyError:
            req.deferred.callback('Not found')
            return
        call.end(req)
        req.deferred.callback('Ok')

    def _CC_debug(self, req):
        pass #FIXME What to do?


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
        
        self.db = CDRDatabase()
        self.monitor = None
        
        self.calls = {}
        self._restore_calls()

    def run(self):
        ## First set up listening on the unix socket
        try:
            gid = grp.getgrnam(self.group)[2]
            mode = 0660
        except KeyError, IndexError:
            mode = 0666
        reactor.listenUNIX(address=self.path, factory=CallControlFactory(self), mode=mode)
        ## Make it writable only to the SIP proxy group members
        try:
            os.chown(self.path, -1, gid)
        except OSError:
            log.warn("couldn't set access rights for %s." % path)
            log.warn("SER may not be able to communicate with us!")
        except NameError:
            pass

        ## Then setup the CallsMonitor
        self.monitor = CallsMonitor(CallControlConfig.checkInterval, self)

        ## And start reactor
        reactor.run()

    def stop(self):
        if self.monitor is not None:
            self.monitor.showdown()
        self.db.close()
        self._save_calls()
        reactor.stop()
    
    def _save_calls():
        if self.calls:
            log.info('saving calls')
            calls_file = '%s/%s' % (process.runtime_directory, calls_file)
            try:
                f = open(calls_file, 'w')
            except:
                pass
            else:
                for call in self.calls.values():
                    call.lock = None
                    ## we will mark timers with 'running' or 'idle', depending on their current state,
                    ## to be able to correctly restore them later (Timer objects cannot be pickled)
                    if call.timer is not None:
                        if call.inprogress:
                            call.timer.cancel()
                            call.timer = 'running' ## temporary mark that this timer was running
                        else:
                            call.timer = 'idle'    ## temporary mark that this timer was not running
                failedDump = False
                try:
                    try:
                        cPickle.dump(self.calls, f)
                    except Exception, why:
                        warning('failed to dump call list: %s' % str(why))
                        failedDump = True
                finally:
                    f.close()
                if failedDump:
                    try:    os.unlink(calls_file)
                    except: pass
            self.calls = {}

    def _restore_calls():
        calls_file = '%s/%s' % (process.runtime_directory, calls_file)
        try:
            f = open(callsFile, 'r')
        except:
            pass
        else:
            try:
                self.calls = cPickle.load(f)
            except Exception, why:
                log.warn('failed to load calls saved in the previous session: %s' % str(why))
            f.close()
            try:    os.unlink(callsFile)
            except: pass
            if self.calls:
                log.info('restoring calls saved from previous session')
                ## the calls in the 2 sets below are never overlapping because closed and terminated
                ## calls have different database fingerprints. so the dictionary update below is safe
                try:
                    terminated = self.db.query(CDRDatabase.CDRTask(None, 'terminated', calls=self.calls))   ## calls terminated by caller/called
                    didtimeout = self.db.query(CDRDatabase.CDRTask(None, 'timedout', calls=self.calls))     ## calls closed by mediaproxy after a media timeout
                except CDRDatabaseError, e:
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
                        if call.timer == 'running':
                            now = time.time()
                            rest = call.starttime + call.timelimit - now
                            if rest < 0:
                                call.timelimit = int(round(now - call.starttime))
                                rest = 0
                            call._setup_timer(rest)
                            call.timer.start()
                        elif call.timer == 'idle':
                            call._setup_timer()
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
                        log.info("removed %d already terminated call%s" % (count, 's'*(count!=1)))

class Request(Structure):
    '''A request parsed into a structure based on request type'''
    __methods = {'init':   ('callid', 'contact', 'cseq', 'diverter', 'ruri', 'sourceip', 'from', 'fromtag', 'to'),
                 'start':  ('callid', 'contact', 'totag'),
                 'update': ('callid', 'cseq', 'fromtag'),
                 'stop':   ('callid',),
                 'debug':  ()}
    def __init__(self, message):
        Structure.__init__(self)
        try:    message + ''
        except: raise ValueError, 'message should be a string'
        lines = [line.strip() for line in message.splitlines() if line.strip()]
        if not lines:
            raise InvalidRequestError, 'missing input'
        cmd = lines[0].lower()
        if cmd not in self.__methods.keys():
            raise InvalidRequestError, 'unknown request: %s' % cmd
        try:
            parameters = dict([re.split(r':\s+', l, 1) for l in lines[1:]])
        except ValueError:
            raise InvalidRequestError, "badly formatted request"
        for p in self.__methods[cmd]:
            try: 
                parameters[p]
            except KeyError:
                raise InvalidRequestError, 'missing %s from request' % p
        self.cmd = cmd
        self.update(parameters)
        if cmd=='init' and self.diverter.lower()=='none':
            self.diverter = None
        self.deferred = defer.Deferred()
    def __str__(self):
        if self.cmd == 'init':
            return "%(cmd)s: callid=%(callid)s from=%(from)s to=%(to)s ruri=%(ruri)s cseq=%(cseq)s diverter=%(diverter)s sourceip=%(sourceip)s" % self
        elif self.cmd == 'start':
            return "%(cmd)s: callid=%(callid)s" % self
        elif self.cmd == 'update':
            return "%(cmd)s: callid=%(callid)s cseq=%(cseq)s fromtag=%(fromtag)s" % self
        elif self.cmd == 'stop':
            return "%(cmd)s: callid=%(callid)s" % self
        elif self.cmd == 'debug':
            return "%(cmd)s" % self
        else:
            return Structure.__str__(self)

