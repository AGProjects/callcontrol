# Copyright (C) 2005-2008 AG Projects.
#

"""CDRDatabase related API"""

import time
import sqlobject

from application.configuration import ConfigSection, ConfigFile
from application.python.queue import EventQueue
from application import log

from twisted.internet import defer, reactor
from twisted.python import failure

from callcontrol import configuration_filename

class RadacctTable(str):
    """A radacct table name"""
    def __init__(self, value):
        str.__init__(self, value)
    def __get__(self, obj, type_=None):
        return time.strftime(self)

class CDRDatabaseConfig(ConfigSection):
    _datatypes = {'table': RadacctTable}
    user           = ''
    password       = ''
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

class CDRDatabaseError(Exception): pass

class CDRDatabase(object):
    """Interface with the CDR database"""
    class CDRTask(object):
        def __init__(self, deferred, tasktype, **kwargs):
            self.deferred = deferred
            self.tasktype = tasktype
            self.args = kwargs

    def __init__(self):
        self.queue = EventQueue(handler=self._handle_task, name='CDRQueue')
        self.queue.start()

        credentials = CDRDatabaseConfig.user and ( "%s%s@" % (CDRDatabaseConfig.user, CDRDatabaseConfig.password and ":%s" % (CDRDatabaseConfig.password) or '') ) or ''
        self.conn = sqlobject.connectionForURI('mysql://%s%s/%s' % (credentials, CDRDatabaseConfig.host, CDRDatabaseConfig.database))

    def close(self):
        self.conn.close()
        self.queue.stop()

    def getTerminatedCalls(self, calls):
        """
        Retrieve those calls from the ones in progress that were already terminated by caller/called.

        Returns a Deferred. Callback will be called with list of call ids.
        """
        deferred = defer.Deferred()
        self.queue.put(CDRDatabase.CDRTask(deferred, 'terminated', calls=calls))
        return deferred

    def getTimedoutCalls(self, calls):
        """
        Retrieve those calls from the ones in progress that did timeout and were closed by mediaproxy.

        Returns a Deferred. Callback will be called with list of call ids.
        """
        deferred = defer.Deferred()
        self.queue.put(CDRDatabase.CDRTask(deferred, 'timedout', calls=calls))
        return deferred

    def query(self, task):
        def _unknown_task(task):
            raise CDRDatabaseError("Got unknown task to handle: %s" % task.tasktype)
        return getattr(self, '_CDR_%s' % task.tasktype, _unknown_task)(task)

    def _handle_task(self, task):
        try:
            reactor.callFromThread(task.deferred.callback, self.query(task))
        except Exception, e:
            reactor.callFromThread(task.deferred.errback, failure.Failure(e))

    def _CDR_terminated(self, task):
        try:
            calls = dict([(call.callid, call) for call in task.args['calls'].values() if call.inprogress])
            if not calls:
                return {}
            ids = "(%s)" % ','.join(calls.keys())
            table = CDRDatabaseConfig.table
            query = """SELECT %(sessionIdField)s AS callid, %(durationField)s AS duration,
                              %(fromTagField)s AS fromtag, %(toTagField)s AS totag
                       FROM   %%(table)s
                       WHERE  %(sessionIdField)s IN %%(ids)s AND
                              %(stopInfoField)s IS NOT NULL""" % CDRDatabaseConfig.__dict__ % locals()
            rows = self.conn.queryAll(query)
            def find(row, calls):
                try:
                    call = calls[row[0]]
                except KeyError:
                    return False
                return call.fromtag==row[2] and call.totag==row[3]
            return dict([(row[0], {'callid': row[0], 'duration': row[1], 'fromtag': row[2], 'totag': row[3]}) for row in rows if find(row, calls)])
        except Exception, e:
            log.error('Query failed: %s' % query)
            raise CDRDatabaseError("Exception while querying for terminated calls %s." % e)

    def _CDR_timedout(self, task):
        try:
            calls = dict([(call.callid, call) for call in task.args['calls'].values() if call.inprogress])
            if not calls:
                return {}
            ids = "(%s)" % ','.join(calls.keys())
            table = CDRDatabaseConfig.table
            query = '''SELECT %(sessionIdField)s AS callid, %(durationField)s AS duration,
                              %(fromTagField)s AS fromtag, %(toTagField)s AS totag
                       FROM   %%(table)s
                       WHERE  %(sessionIdField)s IN %%(ids)s AND
                              %(mediaInfoField)s LIKE 'timeout%%%%' AND
                              %(stopInfoField)s IS NULL''' % CDRDatabaseConfig.__dict__ % locals()
            rows = self.conn.queryAll(query)
            def find(row, calls):
                try:
                    call = calls[row[0]]
                except KeyError:
                    return False
                return call.fromtag==row[2] and call.totag==row[3]
            return dict([(row[0], {'callid': row[0], 'duration': row[1], 'fromtag': row[2], 'totag': row[3]}) for row in rows if find(row, calls)])
        except Exception, e:
            log.error('Query failed: %s' % query)
            raise CDRDatabaseError("Exception while querying for timedout calls %s." % e)
