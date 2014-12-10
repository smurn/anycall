# Copyright (c) 2014 Stefan C. Mueller

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, 
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER 
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING 
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.

import bidict
import uuid
import urlparse
import pickle

from twisted.internet import defer
from twisted.python import log

class RPCSystem(object):
    
    _MESSAGE_TYPE = "RPC"
    
    def __init__(self, connectionpool):
        self._connectionpool = connectionpool
        self._connectionpool.register_type(self._MESSAGE_TYPE)
        
        #: Local functions that may be invoked from remote.
        #: maps `functionid <-> callable`
        self._functions = bidict.bidict()
        
        #: Calls made from remote to here that are currently in progress.
        #: Maps `(peerid, functionid)` -> `Deferred`.
        self._remote_to_local = {}
        
        #: Calls made from here to remote functions.
        #: Maps `functionid` -> `Deferred`.
        self._local_to_remote = {}


    def open(self):
        """
        Opens the port.
        
        :returns: Deferred that callbacks when we are ready to make and receive calls.
        """
        return self._connectionpool.open(self._packet_received)
    
    def close(self):
        """
        Stop listing for new connections and close all open connections.
        
        :returns: Deferred that calls back once everything is closed.
        """
        return self._connectionpool.close()

    def get_function_url(self, function):
        """
        Registers the given callable in the system (if it isn't already)
        and returns the URL that can be used to invoke the given function from remote.
        """
        if function in ~self._functions:
            functionid = self._functions[:function]
        else:
            functionid = uuid.uuid1()
            self._functions[functionid] = function
        return "anycall://%s/functions/%s" % (self._connectionpool.ownid, functionid.hex)


    def create_function_stub(self, url):
        """
        Create a callable that will invoke the given remote function.
        
        The stub will return a deferred even if the remote function does not.
        """
        parseresult = urlparse.urlparse(url)
        scheme = parseresult.scheme
        path = parseresult.path.split("/")
        if scheme != "anycall":
            raise ValueError("Not an anycall URL: %s" % repr(url))
        if len(path) != 3 or path[0] != "" or path[1] != "functions":
            raise ValueError("Not an URL for a remote function: %s" % repr(url))
        try:
            functionid = uuid.UUID(path[2])
        except ValueError:
            raise ValueError("Not a valid URL for a remote function: %s" % repr(url))
        
        return _RPCFunctionStub(parseresult.netloc, functionid, self)
    
    
    def _send(self, peer, obj):
        log.msg("Sending to %s: %s" % (peer, repr(obj)))
        return self._connectionpool.send(peer, self._MESSAGE_TYPE, pickle.dumps(obj, pickle.HIGHEST_PROTOCOL))
    
    def _packet_received(self, peerid, typename, data):
        try:
            if typename != self._MESSAGE_TYPE:
                raise ValueError("Received unexpected packet type:%s" % typename)
            
            obj = pickle.loads(data)
    
            log.msg("Received from %s: %s" % (peerid, repr(obj)))
            
            if isinstance(obj, _Call):
                self._Call_received(peerid, obj)
            elif isinstance(obj, _CallReturn):
                self._CallReturn_received(peerid, obj)
            elif isinstance(obj, _CallFail):
                self._CallFail_received(peerid, obj)
            elif isinstance(obj, _CallCancel):
                self._CallCancel_received(peerid, obj)
            else:
                raise ValueError("Received unknown object type")
        except:
            log.err()

    def _Call_received(self, peerid, obj):
        if obj.functionid not in self._functions:
            raise ValueError("Call for unregistered function.")
        
        func = self._functions[obj.functionid]
        
        d = defer.maybeDeferred(func, *obj.args, **obj.kwargs)
        
        self._remote_to_local[(peerid, obj.callid)] = d
        
        def on_success(retval):
            if (peerid, obj.callid) in self._remote_to_local:
                del self._remote_to_local[(peerid, obj.callid)]
                return self._send(peerid, _CallReturn(obj.callid, retval))
            
        def on_fail(failure):
            if (peerid, obj.callid) in self._remote_to_local:
                del self._remote_to_local[(peerid, obj.callid)]
                return self._send(peerid, _CallFail(obj.callid, failure))
        
        d. addCallbacks(on_success, on_fail)
        
        def uncought(failure):
            log.err(failure)
            
        d.addErrback(uncought)

    def _CallReturn_received(self, peerid, obj):
        try:
            d = self._local_to_remote.pop(obj.callid)
        except KeyError:
            raise ValueError("Received return value for non-existent call.")
        d.callback(obj.retval)
        
    def _CallFail_received(self, peerid, obj):
        try:
            d = self._local_to_remote.pop(obj.callid)
        except KeyError:
            raise ValueError("Received failure for non-existent call.")
        d.errback(obj.failure)
        
    def _CallCancel_received(self, peerid, obj):
        try:
            d = self._remote_to_local.pop((peerid, obj.callid))
            d.cancel()
        except KeyError:
            # We have sent the result already.
            pass
        
    def _invoke_function(self, peerid, functionid, args, kwargs):
        def canceller(d):
            if callid in self._local_to_remote:
                self._send(peerid, _CallCancel(callid))
        
        callid = uuid.uuid1()
        call = _Call(callid, functionid, args, kwargs)
        d_send = self._send(peerid, call)
        
        def send_success(_):
            d = defer.Deferred(canceller)
            self._local_to_remote[callid] = d
            return d
        
        d_send.addCallback(send_success)
        return d_send


class _RPCFunctionStub(object):
    def __init__(self, peerid, functionid, rpcsystem):
        self.peerid = peerid
        self.functionid = functionid
        self.rpcsystem = rpcsystem
    
    def __call__(self, *args, **kwargs):
        return self.rpcsystem._invoke_function(self.peerid, self.functionid, args, kwargs)
    
    def __repr__(self):
        return "RPCStub(%s)" % repr(self.url)
    
    def __str__(self):
        return repr(self)
    
    def __eq__(self, other):
        return self.peerid == other.peerid and self.functionid == other.functionid
    
    def __ne__(self, other):
        return not self.__eq__(other)
    
    def __hash__(self):
        return hash(self.peerid) + hash(self.functionid)


class _Call(object):
    def __init__(self, callid, functionid, args, kwargs):
        self.callid = callid
        self.functionid = functionid
        self.args = args
        self.kwargs = kwargs
    def __repr__(self):
        return "_Call(%s, %s, %s, %s)" %(repr(self.callid), repr(self.functionid), repr(self.args), repr(self.kwargs))
        
class _CallReturn(object):
    def __init__(self, callid, retval):
        self.callid = callid
        self.retval = retval
    def __repr__(self):
        return "_CallReturn(%s, %s)" %(repr(self.callid), repr(self.retval))
        
class _CallFail(object):
    def __init__(self, callid, failure):
        self.callid = callid
        self.failure = failure
    def __repr__(self):
        return "_CallFail(%s, %s)" %(repr(self.callid), repr(self.failure))
    
class _CallCancel(object):
    def __init__(self, callid):
        self.callid = callid
    def __repr__(self):
        return "_CallCancel(%s)" %(repr(self.callid))