#!/usr/bin/env python

"""
Example using ClientPool
========

    import tornado.ioloop
    import tornado.web
    import tornadoasyncmemcache as memcache
    import time
    
    ccs = memcache.ClientPool(['127.0.0.1:11211'], maxclients=100, 
        connect_timeout=0.05,request_timeout=0.1, server_retries = 1, dead_retry = 10)
    
    class MainHandler(tornado.web.RequestHandler):
      @tornado.web.asynchronous
      def get(self):
        time_str = time.strftime('%Y-%m-%d %H:%M:%S')
        ccs.set('test_data', 'Hello world @ %s' % time_str,
                callback=self._get_start, fail_callback = self._on_fail)
    
      def _get_start(self, data):
        ccs.get('test_data', callback=self._get_end, fail_callback = self.on_fail)
    
      def _get_end(self, data):
        self.write(data)
        self.finish()
    
      def _on_fail(self, *args):
        self.send_error(500)
        self.finish()
    
    application = tornado.web.Application([
      (r"/", MainHandler),
    ])
    
    if __name__ == "__main__":
      application.listen(8888)
      tornado.ioloop.IOLoop.instance().start()

"""
import weakref
import sys
import socket
import time
import types
import contextlib
from tornado import iostream, ioloop
from tornado import stack_context
from functools import partial
import collections
from tornado.util import b, bytes_type
try:
    import cPickle as pickle
except ImportError:
    import pickle

__author__    = "Tornadoified: David Novakovic dpn@dpn.name, original code: Evan Martin <martine@danga.com>"
__version__   = "1.0"
__copyright__ = "Copyright (C) 2003 Danga Interactive"
__license__   = "Python"

class TooManyClients(Exception):
    pass

class ClientPool(object):

    CMDS = ('get', 'replace', 'set', 'decr', 'incr', 'delete')

    def __init__(self,
                 servers,
                 mincached = 0,
                 maxcached = 0,
                 maxclients = 0,
                 *args, **kwargs):

        assert isinstance(mincached, int)
        assert isinstance(maxcached, int)
        assert isinstance(maxclients, int)
        if maxclients > 0:
            assert maxclients >= mincached
            assert maxclients >= maxcached
        if maxcached > 0:
            assert maxcached >= mincached

        self._servers = servers
        self._args, self._kwargs = args, kwargs
        self._used = collections.deque()
        self._maxclients = maxclients
        self._mincached = mincached
        self._maxcached = maxcached

        self._clients = collections.deque(self._create_clients(mincached))

    def _create_clients(self, n):
        assert n >= 0
        return [Client(self._servers, *self._args, **self._kwargs)
                for x in xrange(n)]

    def _do(self, cmd, *args, **kwargs):
        fail_callback = None
        if 'fail_callback' in kwargs:
            fail_callback = kwargs['fail_callback']
            del kwargs['fail_callback']
        if not self._clients:
            if self._maxclients > 0 and (len(self._clients)
                + len(self._used) >= self._maxclients):
                fail_reason = "Max of %d clients is already reached" % self._maxclients
                if fail_callback:
                    fail_callback(fail_reason)
                    return
                else:
                    raise TooManyClients(fail_reason)
                self._clients.append(self._create_clients(1)[0])
        c = self._clients.popleft()
        kwargs['callback'] = partial(self._gen_cb, c=c, _cb=kwargs['callback'])
        self._used.append(c)
        context = partial(self._cleanup, fail_callback = partial(self._gen_cb, c=c, _cb= fail_callback))
        with stack_context.StackContext(context):
            getattr(c, cmd)(*args, **kwargs)

    @contextlib.contextmanager
    def _cleanup(self, fail_callback = None):
        try:
            yield
        except _Error as e:
            if fail_callback:
                fail_callback(e.args)

    def __getattr__(self, name):
        if name in self.CMDS:
            return partial(self._do, name)
        raise AttributeError("'%s' object has no attribute '%s'" %
            (self.__class__.__name__, name))
        
    def _gen_cb(self, response, c, _cb, *args, **kwargs):
        self._used.remove(c)
        if self._maxcached == 0 or self._maxcached > len(self._clients):
            self._clients.append(c)
        else:
            c.disconnect_all()
        if _cb:
            _cb(response, *args, **kwargs)
    

class _Error(Exception):
    pass

class Client(object):
    """
    Object representing a pool of memcache servers.
    
    See L{memcache} for an overview.

    In all cases where a key is used, the key can be either:
        1. A simple hashable type (string, integer, etc.).
        2. A tuple of C{(hashvalue, key)}.  This is useful if you want to avoid
        making this module calculate a hash value.  You may prefer, for
        example, to keep all of a given user's objects on the same memcache
        server, so you could use the user's unique id as the hash value.

    @group Setup: __init__, set_servers, forget_dead_hosts, disconnect_all, debuglog
    @group Insertion: set, add, replace
    @group Retrieval: get, get_multi
    @group Integers: incr, decr
    @group Removal: delete
    @sort: __init__, set_servers, forget_dead_hosts, disconnect_all, debuglog,\
           set, add, replace, get, get_multi, incr, decr, delete
    """
    _FLAG_PICKLE  = 1<<0
    _FLAG_INTEGER = 1<<1
    _FLAG_LONG    = 1<<2

    _SERVER_RETRIES = 10  # how many times to try finding a free server.
    
    
    _ASYNC_CLIENTS = weakref.WeakKeyDictionary()

#    def __new__(cls, servers, max_connections=1000, debug=0, io_loop=None):
#        # There is one client per IOLoop since they share curl instances
#        io_loop = io_loop or ioloop.IOLoop.instance()
#        if io_loop in cls._ASYNC_CLIENTS:
#            return cls._ASYNC_CLIENTS[io_loop]
#        else:
#            instance = super(Client, cls).__new__(cls)
#            instance.io_loop = io_loop
#            instance.set_servers(servers)
#            instance.debug = debug
#            instance.stats = {}
#            instance.servers
#            cls._ASYNC_CLIENTS[io_loop] = instance
#            return instance
    
    def __init__(self, servers, debug=0, io_loop=None, connect_timeout = None, request_timeout = None, dead_retry = None, server_retries = None):
        io_loop = io_loop or ioloop.IOLoop.instance()
        self.io_loop = io_loop
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.server_retries = server_retries if server_retries is not None else Client._SERVER_RETRIES
        self.dead_retry = dead_retry
        self._timeout = None
        self.set_servers(servers)
        self.debug = debug
        self.stats = {}
        self.servers
        self._ASYNC_CLIENTS[io_loop] = self

#    def __init__(self, servers, debug=0):
#        """
#        Create a new Client object with the given list of servers.
#
#        @param servers: C{servers} is passed to L{set_servers}.
#        @param debug: whether to display error messages when a server can't be
#        contacted.
#        """
#        self.set_servers(servers)
#        self.debug = debug
#        self.stats = {}

    def _set_timeout(self, server, callback):

        if self.request_timeout:
            def clear_and_call(*args, **kwargs):
                self._clear_timeout()
                callback(*args, **kwargs)
            if self._timeout is not None:
                self.io_loop.remove_timeout(self._timeout)
            self._timeout = self.io_loop.add_timeout(
                    time.time() + self.request_timeout,
                    stack_context.wrap(partial(self._on_timeout, server)))
            return clear_and_call
        else:
            return callback

    def _clear_timeout(self):
        if self._timeout is not None:
            self.io_loop.remove_timeout(self._timeout)
            self._timeout = None
    
    def _on_timeout(self, server):
        self._timeout = None
        server.mark_dead('Time out')
        raise _Error('memcache call timeout')
    
    def set_servers(self, servers):
        """
        Set the pool of servers used by this client.

        @param servers: an array of servers.
        Servers can be passed in two forms:
            1. Strings of the form C{"host:port"}, which implies a default weight of 1.
            2. Tuples of the form C{("host:port", weight)}, where C{weight} is
            an integer weight value.
        """
        kwargs = dict(io_loop = self.io_loop)
        #if self.connect_timeout:
        #    kwargs['connect_timeout'] = self.connect_timeout 
        if self.dead_retry:
            kwargs['dead_retry'] = self.dead_retry 
        self.servers = [_Host(s, self.debuglog, **kwargs) for s in servers]
        self._init_buckets()

    def debuglog(self, str):
        if self.debug:
            sys.stderr.write("MemCached: %s\n" % str)

    def _statlog(self, func):
        if not self.stats.has_key(func):
            self.stats[func] = 1
        else:
            self.stats[func] += 1

    def forget_dead_hosts(self):
        """
        Reset every host in the pool to an "alive" state.
        """
        for s in self.servers:
            s.dead_until = 0

    def _init_buckets(self):
        self.buckets = []
        for server in self.servers:
            for i in range(server.weight):
                self.buckets.append(server)

    def _get_server(self, key, connect_callback, *args, **kwargs):
        if type(key) == types.TupleType:
            serverhash = key[0]
            key = key[1]
        else:
            serverhash = hash(key)
        
        retry = kwargs['retry'] if 'retry' in kwargs else 0
        server = kwargs['server'] if 'server' in kwargs else None

        if retry > 0:
            server.mark_dead('connect failed')
            if retry >= self.server_retries:
                self._clear_timeout()
                raise _Error('no server available')
            serverhash = hash(str(serverhash)+str(retry))

        server = self.buckets[serverhash % len(self.buckets)]
        if self.connect_timeout:
            if self._timeout is not None:
                self.io_loop.remove_timeout(self._timeout)
            self._timeout = self.io_loop.add_timeout(
                    time.time() + self.connect_timeout,
                    stack_context.wrap(partial(self._on_timeout, server)))
        server.connect(callback = partial(connect_callback, server, key, *args), fail_callback = 
                partial(self._get_server, (serverhash, key), connect_callback, *args, server = server, retry = retry +1))

    def disconnect_all(self):
        for s in self.servers:
            s.close_socket()
    
    def delete(self, key, time=0, callback=None):
        '''Deletes a key from the memcache.
        
        @return: Nonzero on success.
        @rtype: int
        '''
        self._get_server(key, self._real_delete, time, callback)

    def _real_delete(self, server, key, time, callback):
        if not server:
            self.finish(partial(callback,0))
        self._statlog('delete')
        if time != None:
            cmd = "delete %s %d" % (key, time)
        else:
            cmd = "delete %s" % key

        server.send_cmd(cmd, callback=partial(self._delete_send_cb,server, self._set_timeout(server, callback)))
        
    def _delete_send_cb(self, server, callback):
        server.expect("DELETED",callback=partial(self._expect_cb, callback=callback))
    
        
#        except socket.error, msg:
#            server.mark_dead(msg[1])
#            return 0
#        return 1

    def incr(self, key, delta=1, callback=None):
        """
        Sends a command to the server to atomically increment the value for C{key} by
        C{delta}, or by 1 if C{delta} is unspecified.  Returns None if C{key} doesn't
        exist on server, otherwise it returns the new value after incrementing.

        Note that the value for C{key} must already exist in the memcache, and it
        must be the string representation of an integer.

        >>> mc.set("counter", "20")  # returns 1, indicating success
        1
        >>> mc.incr("counter")
        21
        >>> mc.incr("counter")
        22

        Overflow on server is not checked.  Be aware of values approaching
        2**32.  See L{decr}.

        @param delta: Integer amount to increment by (should be zero or greater).
        @return: New value after incrementing.
        @rtype: int
        """
        self._incrdecr("incr", key, delta, callback=callback)

    def decr(self, key, delta=1, callback=None):
        """
        Like L{incr}, but decrements.  Unlike L{incr}, underflow is checked and
        new values are capped at 0.  If server value is 1, a decrement of 2
        returns 0, not -1.

        @param delta: Integer amount to decrement by (should be zero or greater).
        @return: New value after decrementing.
        @rtype: int
        """
        self._incrdecr("decr", key, delta, callback=callback)

    def _incrdecr(self, cmd, key, delta, callback):
        self._get_server(key, self._real_incrdecr, cmd, delta, callback)

    def _real_incrdecr(self, server, key, cmd, delta, callback):
        if not server:
            self.finish(partial(callback, 0))
        self._statlog(cmd)
        cmd = "%s %s %d" % (cmd, key, delta)

        server.send_cmd(cmd, callback=partial(self._incrdecr_send_cb,server, self._set_timeout(server, callback)))
        
    def _send_incrdecr_cb(self, server, callback):
        server.readline(callback=partial(self._send_incrdecr_check_cb, callback=callback))
    
    def _send_incrdecr_check_cb(self, line, callback):
        self.finish(partial(callback,int(line)))
        
#        except socket.error, msg:
#            server.mark_dead(msg[1])
#            return None

    def add(self, key, val, time=0, callback=None):
        '''
        Add new key with value.
        
        Like L{set}, but only stores in memcache if the key doesn't already exist.

        @return: Nonzero on success.
        @rtype: int
        '''
        self._set("add", key, val, time, callback)
    def replace(self, key, val, time=0, callback=None):
        '''Replace existing key with value.
        
        Like L{set}, but only stores in memcache if the key already exists.  
        The opposite of L{add}.

        @return: Nonzero on success.
        @rtype: int
        '''
        self._set("replace", key, val, time, callback)
    def set(self, key, val, time=0, callback=None):
        '''Unconditionally sets a key to a given value in the memcache.

        The C{key} can optionally be an tuple, with the first element being the
        hash value, if you want to avoid making this module calculate a hash value.
        You may prefer, for example, to keep all of a given user's objects on the
        same memcache server, so you could use the user's unique id as the hash
        value.

        @return: Nonzero on success.
        @rtype: int
        '''
        self._set("set", key, val, time, callback)
    
    def _set(self, cmd, key, val, time, callback):
        self._get_server(key, self._real_set, cmd, val, time, callback)

    def _real_set(self, server, key, cmd, val, time, callback):
        if not server:
            self.finish(partial(callback,0))

        self._statlog(cmd)

        flags = 0
        if isinstance(val, types.StringTypes):
            pass
        elif isinstance(val, int):
            flags |= Client._FLAG_INTEGER
            val = "%d" % val
        elif isinstance(val, long):
            flags |= Client._FLAG_LONG
            val = "%d" % val
        else:
            flags |= Client._FLAG_PICKLE
            val = pickle.dumps(val, 2)
        
        fullcmd = "%s %s %d %d %d\r\n%s" % (cmd, key, flags, time, len(val), val)
        
        server.send_cmd(fullcmd, callback=partial(self._set_send_cb, server=server, callback=self._set_timeout(server, callback)))
        
    def _set_send_cb(self, server, callback):
        server.expect("STORED", callback=partial(self._expect_cb, value=None, callback=callback))
#        except socket.error, msg:
#            server.mark_dead(msg[1])
#            return 0
#        return 1

    def get(self, key, callback):
        '''Retrieves a key from the memcache.
        
        @return: The value or None.
        '''
        self._get_server(key, self._real_get, callback)

    def _real_get(self, server, key, callback):
        if not server:
            self._clear_timeout()
            raise _Error('No available server for %s' % key)

        self._statlog('get')

        server.send_cmd("get %s" % key, partial(self._get_send_cb, server=server, callback=self._set_timeout(server, callback)))
        
    def _get_send_cb(self, server, callback):
        self._expectvalue(server, line=None, callback=partial(self._get_expectval_cb, server=server, callback=callback))
    
    def _get_expectval_cb(self, rkey, flags, rlen, server, callback):
        if not rkey:
            self.finish(partial(callback,None))
            return
        self._recv_value(server, flags, rlen, partial(self._get_recv_cb, server=server, callback=callback))
        
    def _get_recv_cb(self, value, server, callback):
        server.expect("END", partial(self._expect_cb, value=value, callback=callback))
        
    def _expect_cb(self, expected=None, value=None, callback=None):
#        print "in expect cb"
        self.finish(partial(callback,value))
#        except (_Error, socket.error), msg:
#            if type(msg) is types.TupleType:
#                msg = msg[1]
#            server.mark_dead(msg)
#            return None
#        return value

    # commented out sendmulti, as it would bea easier for the caller
    # to just send an appropriate callback with multipl requests.

    def _expectvalue(self, server, line=None, callback=None):
        if not line:
            server.readline(partial(self._expectvalue_cb, callback=callback))
        else:
            self._expectvalue_cb(line, callback)

    def _expectvalue_cb(self, line, callback):
        if line[:5] == 'VALUE':
            resp, rkey, flags, len = line.split()
            flags = int(flags)
            rlen = int(len)
            callback(rkey, flags, rlen)
        else:
            callback(None, None, None)

    def _recv_value(self, server, flags, rlen, callback):
        rlen += 2 # include \r\n
        server.recv(rlen, partial(self._recv_value_cb,rlen=rlen, flags=flags, callback=callback))
        
        
    def _recv_value_cb(self, buf, flags, rlen, callback):
        if len(buf) != rlen:
            self._clear_timeout()
            raise _Error("received %d bytes when expecting %d" % (len(buf), rlen))

        if len(buf) == rlen:
            buf = buf[:-2]  # strip \r\n

        if flags == 0:
            val = buf
        elif flags & Client._FLAG_INTEGER:
            val = int(buf)
        elif flags & Client._FLAG_LONG:
            val = long(buf)
        elif flags & Client._FLAG_PICKLE:
            val = pickle.loads(buf)
        else:
            self.debuglog("unknown flags on get: %x\n" % flags)

        self.finish(partial(callback,val))
        
    def finish(self, callback):
        callback()
#        self.disconnect_all()

    
class _Host:
    _DEAD_RETRY = 30  # number of seconds before retrying a dead server.

    def __init__(self, host, debugfunc=None, io_loop = None, dead_retry = None):
        if isinstance(host, types.TupleType):
            host = host[0]
            self.weight = host[1]
        else:
            self.weight = 1

        if host.find(":") > 0:
            self.ip, self.port = host.split(":")
            self.port = int(self.port)
        else:
            self.ip, self.port = host, 11211

        if not debugfunc:
            debugfunc = lambda x: x
        self.debuglog = debugfunc
        self.dead_retry = dead_retry if dead_retry is not None else _Host._DEAD_RETRY

        self.io_loop = io_loop if io_loop is not None else ioloop.IOLoop.instance()

        self.deaduntil = 0
        self.stream = None
    
    def _check_dead(self):
        if self.deaduntil and self.deaduntil > time.time():
            return 1
        self.deaduntil = 0
        return 0

    def connect(self, callback, fail_callback = None):
        return self._get_socket(callback, fail_callback)

    def mark_dead(self, reason):
        print "MemCache: %s: %s.  Marking dead." % (self, reason)
        if self.deaduntil == 0:
	    self.deaduntil = time.time() + self.dead_retry
        self.close_socket()
        
    def _get_socket(self, callback, fail_callback = None):
        if self._check_dead():
            if fail_callback:
                fail_callback()
                return None
            else:
                return None
        if self.stream:
            callback()
            return None

        addrinfo = socket.getaddrinfo(self.ip, self.port, socket.AF_INET, socket.SOCK_STREAM, 0, 0)
        af, socktype, proto, canonname, sockaddr = addrinfo[0]
        self.stream = iostream.IOStream(socket.socket(af, socktype, proto),
                io_loop = self.io_loop)
        self.stream.debug=True
        self.stream.connect(sockaddr, callback)
        return None
    
    def close_socket(self):
        if self.stream:
#            self.socket.close()
            self.stream.close()
            self.stream = None

    def send_cmd(self, cmd, callback):
#        print "in sendcmd", repr(cmd), callback
        self.stream.write(cmd+"\r\n", callback)
        #self.socket.sendall(cmd + "\r\n")

    def readline(self, callback):
        self.stream.read_until("\r\n", callback)

    def expect(self, text, callback):
        self.readline(partial(self._expect_cb, text=text, callback=callback))
        
    def _expect_cb(self, data, text, callback):
        if data != text:
            self.debuglog("while expecting '%s', got unexpected response '%s'" % (text, data))
        callback(data)
    
    def recv(self, rlen, callback):
        self.stream.read_bytes(rlen, callback)
        
    def __str__(self):
        d = ''
        if self.deaduntil:
            d = " (dead until %d)" % self.deaduntil
        return "%s:%d%s" % (self.ip, self.port, d)

def _doctest():
    import doctest, memcache
    servers = ["127.0.0.1:11211"]
    mc = Client(servers, debug=1)
    globs = {"mc": mc}
    return doctest.testmod(memcache, globs=globs)

if __name__ == "__main__":
    print "Testing docstrings..."
    _doctest()
    print "Running tests:"
    print
    #servers = ["127.0.0.1:11211", "127.0.0.1:11212"]
    servers = ["127.0.0.1:11211"]
    mc = Client(servers, debug=1)

    def to_s(val):
        if not isinstance(val, types.StringTypes):
            return "%s (%s)" % (val, type(val))
        return "%s" % val
    def test_setget(key, val):
        print "Testing set/get {'%s': %s} ..." % (to_s(key), to_s(val)),
        mc.set(key, val)
        newval = mc.get(key)
        if newval == val:
            print "OK"
            return 1
        else:
            print "FAIL"
            return 0

    class FooStruct:
        def __init__(self):
            self.bar = "baz"
        def __str__(self):
            return "A FooStruct"
        def __eq__(self, other):
            if isinstance(other, FooStruct):
                return self.bar == other.bar
            return 0
        
    test_setget("a_string", "some random string")
    test_setget("an_integer", 42)
    if test_setget("long", long(1<<30)):
        print "Testing delete ...",
        if mc.delete("long"):
            print "OK"
        else:
            print "FAIL"
    print "Testing get_multi ...",
    print mc.get_multi(["a_string", "an_integer"])

    print "Testing get(unknown value) ...",
    print to_s(mc.get("unknown_value"))

    f = FooStruct()
    test_setget("foostruct", f)

    print "Testing incr ...",
    x = mc.incr("an_integer", 1)
    if x == 43:
        print "OK"
    else:
        print "FAIL"

    print "Testing decr ...",
    x = mc.decr("an_integer", 1)
    if x == 42:
        print "OK"
    else:
        print "FAIL"



# vim: ts=4 sw=4 softtabstop=4 et :
