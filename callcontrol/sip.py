'''
 Implementation of a SIP Null client

 The SIP Null client will send fake BYE messages and will receive their replies,
but will ignore them completely.

Copyright 2005 AG Projects
'''

import sys, atexit
import socket, errno
import asyncore
from time import sleep

from threading import Thread, Lock
from threading import enumerate as enumerate_threads

from application.configuration import ConfigSection, ConfigFie

from callcontrol.datatypes import NetworkAddress, EndpointAddress
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
    fatal('listening address for the SIP client is not defined')
if SipConfig.proxy is None:
    fatal('SIP proxy address is not defined')

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

try:   socket_map
except NameError: socket_map = {}

class SipError(Exception): pass
class SipClientError(SipError): pass
class SipTransmisionError(SipError): pass

class SipReplyHandler(Thread):
    '''Run the SIP client's reply loop handler in a separate thread'''
    def __init__(self, sipclient):
        Thread.__init__(self, name='SipReplyHandler')
        self.setDaemon(True)
        self.sipclient = sipclient
    def run(self):
        self.sipclient.loop()

## socket_map access is not protected by locks in asyncore, so basically
## the only function that is safe to call from other threads is `send'
## anything that changes socket_map (like close) shouldn't be called.
## if multiple instances of this class are created, they all must be
## created before calling any of their start methods.
class SipNullClient(asyncore.dispatcher):
    '''
    A dumb SIP client, that is able to send a SIP request and wait for the
    reply, which it'll ignore. The SIP request must be build by the caller.
    '''
    def __init__(self, listen=None, proxy=None, socket_map=None):
        asyncore.dispatcher.__init__(self)
        if socket_map is not None:
            self.socket_map = socket_map
        else:
            self.socket_map = {}
        self.listen = listen or SipConfig.listen
        self.proxy  = proxy  or SipConfig.proxy
        self.create_socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.set_reuse_addr()
        try:
            self.bind(self.listen)
        except socket.error, why:
            raise SipClientError, "couldn't create command socket: %s" % why[1]
        self.connected = True
        self.started = False
        self.lock = Lock()
        atexit.register(self.handle_close)

    def send(self, data):
        self.lock.acquire()
        try:
            return self.socket.sendto(data, self.proxy)
        finally:
            self.lock.release()

    def send2(self, data):
        self.lock.acquire()
        try:
            for c in range(0, 3):
                try:
                    return self.socket.sendto(data, self.proxy)
                except socket.error, why:
                    if why[0] != errno.EWOULDBLOCK:
                        raise SipTransmisionError, why[1]
                    #else:
                        #sleep(1e-6)
            raise SipTransmisionError, "Failed to send BYE message after 3 attempts"
        finally:
            self.lock.release()

    def add_channel(self):
        asyncore.dispatcher.add_channel(self, self.socket_map)
    def del_channel(self):
        asyncore.dispatcher.del_channel(self, self.socket_map)
    def writable(self):
        return 0
    def handle_connect(self):
        pass
    def handle_close(self):
        self.close()

    def handle_read(self):
        self.lock.acquire()
        try:
            try:
                self.recv(65536) ## ignore reply
            except socket.error:
                pass
        finally:
            self.lock.release()

    def loop(self):
        asyncore.loop(timeout=10.0, map=self.socket_map)

    def start(self):
        '''Run the reply handler in a separate thread'''
        if not self.started:
            ## We must be careful to run a single thread for each socket_map
            ## to avoid concurency issues because asyncore doesn't properly
            ## secure access to socket_map for multithreaded applications
            for t in enumerate_threads():
                try:
                    socket_map = t.sipclient.socket_map
                except AttributeError:
                    continue
                if socket_map is self.socket_map:
                    ## There is already a thread running for this socket_map.
                    break
            else:
                ## We couldn't find a running thread for this socket_map. Start one.
                SipReplyHandler(self).start()
            self.started = True

class SipClient(object):
    '''Return a singleton instance of SipNullClient, using default parameters.'''
    _lock = Lock()
    _sipclient = None
    def __new__(typ):
        SipClient._lock.acquire()
        try:
            if SipClient._sipclient is None:
                SipClient._sipclient = SipNullClient()
            return SipClient._sipclient
        finally:
            SipClient._lock.release()


class SipClientInfo(Structure):
    '''Describes the SIP client/proxy/connection parameters'''
    def __init__(self):
        Structure.__init__(self)
        self.name    = 'Call Controller'
        self.address = '%s:%d' % SipConfig._sending_address
        self.proxy   = '%s:%d' % SipConfig.proxy

sipClientInfo = SipClientInfo()

