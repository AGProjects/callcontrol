# Copyright (C) 2005-2008 AG Projects.
#

"""CDRDatabase related API"""

import time
import sqlobject

from application.configuration import ConfigSection, ConfigFile

from twisted.internet import defer

from callcontrol import configuration_filename

class RadacctTable(str):
    '''A radacct table name'''
    def __init__(self, value):
        str.__init__(self, value)
    def __get__(self, obj, type_=None):
        return time.strftime(self)

class CDRDatabaseConfig(ConfigSection):
    _dataTypes = {'table': RadacctTable}
    user           = None
    password       = None
    host           = 'localhost'
    database       = 'radius'
    table          = RadacctTable('radacct%Y%m')
    sessionIdField = 'AcctSessionId'
    durationField  = 'AcctSessionTime'
    stopTimeField  = 'AcctStopTime'
    stopInfoField  = 'ConnectInfo_stop'
    mediaInfoField = 'MediaInfo'
    fromTagField   = 'SipFromTag'
    toTagField     = 'SipToTag'

config_file = ConfigFile(configuration_filename)
config_file.read_settings('CDRDatabase', CDRDatabaseConfig)


class CDRDatabase(object):
    '''Interface with the CDR database'''
    class CDRTask(object):
        def __init__(self, deferred, tasktype):
            self.deferred = deferred
            self.tasktype = tasktype

    def __init__(self):
        self.queue = EventQueue(handler=self._handle_task, name='CDRQueue')
        self.queue.start()

        credentials = CDRDatabaseConfig.user and ( "%s%s@" % (CDRDatabaseConfig.user, CDRDatabaseConfig.password and ":%s" % (CDRDatabaseConfig.password) or '') ) or ''
        self.conn = sqlobject.connectionForURI('mysql://%s%s/%s' % (credentials, CDRDatabaseConfig.host, CDRDatabaseConfig.database))

    def __del__(self):
        self.conn.close()
        self.queue.stop()

    def getTerminatedCalls(self):
        '''
        Retrieve those calls from the ones in progress that were already terminated by caller/called.

        Returns a Deferred. Callback will be called with list of call ids.
        '''
        deferred = defer.Deferred()
        self.queue.put(CDRTask(deferred, 'terminated'))
        return deferred

    def getTimedoutCalls(self):
        '''
        Retrieve those calls from the ones in progress that did timeout and were closed by mediaproxy.

        Returns a Deferred. Callback will be called with list of call ids.
        '''
        deferred = defer.Deferred()
        self.queue.put(CDRTask(deferred, 'timedout'))
        return deferred

    def _handle_task(self, task):
        pass #FIXME
