# Copyright (C) 2005-2008 AG Projects.
#

"""Implementation of a call control server for SER."""

import os
import grp

from application.configuration import ConfigSection, ConfigFile
from application.python.queue import EventQueue
from application.process import process

from twisted.internet.protocol import Factory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.internet import reactor, defer

from callcontrol.scheduler import RecurrentCall, KeepRunning
from callcontrol.cdrdb import CDRDatabase
from callcontrol import configuration_filename


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
        deferred = self.application.db.getTerminatedCalls()
        deferred.addCallbacks(callback=self._clean_calls, errback=self._err_handle)
        deferred = self.application.db.getTimedoutCalls()
        deferred.addCallbacks(callback=self._clean_calls, errback=self._err_handle)

        ## Also do the rest of the checking
        pass #FIXME

    def __del__(self):
        self.reccall.cancel()

    def _clean_calls(self, calls):
        pass #FIXME

    def _err_handle(self, failure):
        pass #FIXME


class CallControlProtocol(LineOnlyReceiver):
    def lineReceived(self, line):
        pass #FIXME


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
        
        self.calls = {}
        self._restore_calls()

    def run(self):
        ## First set up listening on the unix socket
        reactor.listenUNIX(address=self.path, factory=CallControlFactory(self), mode=0700)
        try:
            gid = grp.getgrnam(self.group)[2]
        except KeyError, IndexError:
            ## Make it world writable
            try:
                os.chmod(self.path, 0777)
            except:
                log.warn("couldn't set access rights for %s." % path)
                log.warn("SER may not be able to communicate with us!")
        else:
            ## Make it writable only to the SIP proxy group members
            try:
                os.chown(self.path, -1, gid)
                os.chmod(self.path, 0770)
            except OSError:
                log.warn("couldn't set access rights for %s." % path)
                log.warn("SER may not be able to communicate with us!")

        ## Then setup the CallsMonitor
        self.monitor = CallsMonitor(CallControlConfig.checkInterval, self)

        ## And start reactor
        reactor.run()

    def stop(self):
        del self.monitor
        del self.db
        self._save_calls()
        reactor.stop()
    
    def _save_calls():
        if self.calls:
            log.info('saving calls')
            calls_file = '%s/%s' % (process.runtime_directory, 'calls.dat')
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
        pass

