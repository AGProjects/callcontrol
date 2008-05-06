'''Prepaid engine interface implementation.

Copyright 2005 AG Projects
'''

import socket, errno
import random
from threading import RLock, Timer
from configuration import readSettings, ConfigSection
from datatypes import EndpointAddress
from utilities import *

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

# Todo. Rewrite this in a generic way (describe multiple providers in a scalable way) -Dan
class VoipsterPrepaidConfig(ConfigSection):
    _dataTypes = {'address': PrepaidEngineAddress}
    address = ('127.0.0.1', 9024)
    connections = 0

## We use this to overwrite some of the settings above on a local basis if needed
readSettings('PrepaidEngine', PrepaidConfig)
readSettings('VoipsterPrepaidEngine', VoipsterPrepaidConfig)


class PrepaidError(Exception): pass
class PrepaidEngineError(PrepaidError): pass

class PrepaidEngine(object):
    '''One shot connection to the rating engine. Connection is established only during the request'''

    _reply_size = 8192
    _timeout = 3

    def __init__(self, address=None):
        self.address = address or PrepaidConfig.address

    def _execute(self, cmd):
        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        conn.settimeout(1)
        flags = conn.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, flags|1)
        try:
            conn.connect(self.address)
        except (socket.error, socket.gaierror, socket.herror, socket.timeout), why:
            conn.close()
            raise PrepaidEngineError, why
        conn.settimeout(self._timeout)
        try:
            try:
                conn.send(cmd)
                res = conn.recv(self._reply_size)
                if res == '':
                    raise PrepaidEngineError, 'connection closed'
                return res
            except (socket.error, socket.gaierror, socket.herror, socket.timeout), why:
                raise PrepaidEngineError, why
        finally:
            conn.close()

    def execute(self, cmd):
        try:
            return self._execute(cmd)
        except PrepaidEngineError:
            return ''

    def getCallLimit(self, call):
        cmd = 'MaxSessionTime CallId=%(callid)s From=%(billingParty)s To=%(ruri)s Gateway=%(sourceip)s Duration=36000 Lock=1\n' % call
        try:
            res = self._execute(cmd)
        except PrepaidEngineError:
            warning("rating engine failed query: %s" % cmd)
            return 0
        try:
            limit = res.splitlines()[0].strip().capitalize()
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
            error("invalid reply from rating engine: `%s'" % res)
            limit = 0
        return limit

    def debitBalance(self, call):
        cmd = 'DebitBalance CallId=%(callid)s From=%(billingParty)s To=%(ruri)s Gateway=%(sourceip)s Duration=%(duration)s\n' % call
        validAnswers = ('Ok', 'Failed', 'Not prepaid')
        try:
            res = self._execute(cmd)
        except PrepaidEngineError:
            warning("rating engine failed query: %s" % cmd)
            return
        result = res.splitlines()[0].strip().capitalize()
        if result not in validAnswers:
            error("invalid reply from rating engine: `%s'" % result)
            warning("rating engine possible failed query: %s" % cmd)
        elif result == 'Failed':
            warning("rating engine failed query: %s" % cmd)


class PersistentPrepaidEngine(PrepaidEngine):
    '''Persistent prepaid connection. Connection is kept alive between requests, auto reconnecting if needed.'''

    _retry_interval = 10.0
    _reply_size = 8192
    _timeout = 3

    def __init__(self, address=None):
        self.lock = RLock()
        self.address = address or PrepaidConfig.address
        self.conn = None
        self.__connect()
        if self.conn is None:
            warning('connection with rating engine cannot be established')

    def __connect(self):
        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        conn.settimeout(1)
        flags = conn.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, flags|1)
        try:
            conn.connect(self.address)
        except (socket.error, socket.gaierror, socket.herror, socket.timeout), why:
            conn.close()
            self.conn = None
            timer = Timer(self._retry_interval, self.__reconnect)
            timer.setName('ReconnectTimer')
            timer.setDaemon(True)
            timer.start()
        else:
            conn.settimeout(self._timeout)
            self.conn = conn

    def __reconnect(self):
        self.lock.acquire()
        try:
            if self.conn is None:
                #info('trying to reconnect to rating engine')
                self.__connect()
                if self.conn is not None:
                    info('connection with rating engine restored')
        finally:
            self.lock.release()

    def _execute(self, cmd):
        self.lock.acquire()
        try:
            if self.conn is None:
                raise PrepaidEngineError, 'not connected'
            for c in range(0, 3):
                try:
                    self.conn.send(cmd)
                    res = self.conn.recv(self._reply_size)
                    if res == '':
                        # do we need here to make sure it's closed using select.select([f], [], [f], timeout)?
                        raise socket.error, (errno.EPIPE, 'connection closed')
                    return res
                except (socket.error, socket.gaierror, socket.herror, socket.timeout), why:
                    warning('connection with the rating engine died. trying to reconnect...')
                    self.conn.close()
                    self.__connect()
                    if self.conn is None:
                        warning('reconnecting with the rating engine failed')
                        raise PrepaidEngineError, 'connection lost'
                    else:
                        info('successfully reconnected to the rating engine')
            raise PrepaidEngineError, "couldn't re-establish lost connection after 3 attempts"
        finally:
            self.lock.release()


#__connections = [PersistentPrepaidEngine() for x in range(0, PrepaidConfig.connections)]

__connections = {
    'default':          [PersistentPrepaidEngine() for x in range(0, PrepaidConfig.connections)],
    'sip.voipster.com': [PersistentPrepaidEngine(VoipsterPrepaidConfig.address) for x in range(0, VoipsterPrepaidConfig.connections)]
    }


def getConnection(provider):
    return random.choice(__connections.get(provider, __connections['default']))

class PrepaidEngineConnection(object):
    def __new__(typ, provider='default'):
        return getConnection(provider)

