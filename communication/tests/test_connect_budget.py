"""Pure clock/socket/selector fixtures: never resolve DNS, open sockets or sleep."""
import errno
import math
import socket
import threading
from types import SimpleNamespace

import pytest

from communication import channel as transport
from communication.errors import ChannelClosed, ChannelTimeout, TaskCancelled


class FakeSocket:
    def __init__(self, mode='pending', error=0):
        self.mode, self.error = mode, error
        self.closed = False
        self.connects = 0
        self.options = []
        self.timeout = None
        self.on_connect = lambda: None
        self.fail = None

    def setblocking(self, value):
        assert value is False
        if self.fail == 'setblocking': raise OSError('fixture setup failure')

    def connect(self, address):
        self.connects += 1
        self.on_connect()
        if self.mode == 'pending': raise BlockingIOError(errno.EINPROGRESS, 'pending')
        if self.mode == 'interrupted': raise InterruptedError(errno.EINTR, 'interrupted')
        if self.mode == 'refused': raise ConnectionRefusedError('fixture refusal')

    def getsockopt(self, *args):
        if self.fail == 'getsockopt': raise OSError('fixture SO_ERROR failure')
        return self.error

    def setsockopt(self, *args):
        self.options.append(args)
        if self.fail == 'setsockopt': raise OSError('fixture option failure')

    def settimeout(self, value):
        self.timeout = value
        if self.fail == 'settimeout': raise OSError('fixture timeout setup failure')

    def close(self): self.closed = True
    def shutdown(self, *_): pass


@pytest.fixture
def io(monkeypatch):
    state = SimpleNamespace(now=1000., mono=0., resolve_calls=0, sockets=[],
        used=[], steps=[], waits=[], selectors=[], select_failure=None,
        register_failure=None, resolve_hook=lambda: None)

    def advance(seconds):
        state.now += seconds
        state.mono += seconds
    state.advance = advance
    state.addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('192.0.2.1', 9000))]

    def resolve(*args):
        state.resolve_calls += 1
        state.resolve_hook()
        return state.addresses

    def create(*args):
        assert state.sockets, 'unexpected socket/reconnect'
        result = state.sockets.pop(0)
        state.used.append(result)
        return result

    class Selector:
        def __init__(self): self.closed = False; state.selectors.append(self)
        def __enter__(self): return self
        def __exit__(self, *_): self.closed = True
        def register(self, sock, events):
            assert events == transport.selectors.EVENT_WRITE
            if state.register_failure: raise state.register_failure
        def select(self, wait):
            state.waits.append(wait)
            if state.select_failure: raise state.select_failure
            seconds, ready, hook = state.steps.pop(0) if state.steps else (wait, False, lambda: None)
            advance(seconds)
            hook()
            return [('fixture-ready', 2)] if ready else []

    monkeypatch.setattr(transport, 'time', SimpleNamespace(time=lambda: state.now, monotonic=lambda: state.mono))
    monkeypatch.setattr(transport, 'socket', SimpleNamespace(getaddrinfo=resolve, socket=create,
        SOCK_STREAM=socket.SOCK_STREAM, SOL_SOCKET=socket.SOL_SOCKET, SO_ERROR=socket.SO_ERROR,
        IPPROTO_TCP=socket.IPPROTO_TCP, TCP_NODELAY=socket.TCP_NODELAY, SHUT_RDWR=socket.SHUT_RDWR))
    monkeypatch.setattr(transport, 'selectors', SimpleNamespace(DefaultSelector=Selector, EVENT_WRITE=2))
    return state


def dial(**kwargs):
    return transport.Channel.connect(('fixture.invalid', 9000), **kwargs)


@pytest.mark.parametrize('cause', ['cancel', 'deadline', 'zero-budget'])
def test_precheck_never_resolves_or_opens(io, cause):
    cancel = threading.Event()
    if cause == 'cancel': cancel.set()
    with pytest.raises(TaskCancelled if cause == 'cancel' else ChannelTimeout):
        dial(cancel=cancel, deadline=999 if cause == 'deadline' else None,
             connect_timeout=0 if cause == 'zero-budget' else 10)
    assert io.resolve_calls == 0 and not io.used


@pytest.mark.parametrize('cause', ['timeout', 'deadline', 'cancel'])
def test_resolution_is_not_preempted_but_checked_before_open(io, cause):
    cancel = threading.Event()
    io.resolve_hook = (lambda: cancel.set()) if cause == 'cancel' else lambda: io.advance(2)
    with pytest.raises(TaskCancelled if cause == 'cancel' else ChannelTimeout):
        dial(cancel=cancel, connect_timeout=1, deadline=1001 if cause == 'deadline' else None)
    assert io.resolve_calls == 1 and not io.used


@pytest.mark.parametrize('budget', ['timeout', 'deadline'])
def test_pending_connect_uses_earliest_budget_and_closes(io, budget):
    sock = FakeSocket(); io.sockets = [sock]
    with pytest.raises(ChannelTimeout):
        dial(connect_timeout=.1 if budget == 'timeout' else 10,
             deadline=1000.1 if budget == 'deadline' else None)
    assert io.waits == pytest.approx([.1])
    assert sock.closed and sock.connects == 1 and all(s.closed for s in io.selectors)


@pytest.mark.parametrize('ready', [False, True])
def test_cancel_during_connect_closes_even_a_late_writable_socket(io, ready):
    cancel = threading.Event(); sock = FakeSocket(); io.sockets = [sock]
    io.steps = [(.01, ready, cancel.set)]
    with pytest.raises(TaskCancelled): dial(cancel=cancel)
    assert io.waits == [.25] and sock.connects == 1 and sock.closed
    assert sock.options == [] and all(s.closed for s in io.selectors)


def test_late_writable_after_deadline_is_not_delivered(io):
    sock = FakeSocket(); io.sockets = [sock]
    io.steps = [(.3, True, lambda: None)]
    with pytest.raises(ChannelTimeout, match='deadline'): dial(deadline=1000.1)
    assert sock.closed and not sock.options


def test_candidates_share_one_budget(io):
    first, second = FakeSocket('refused'), FakeSocket()
    first.on_connect = lambda: io.advance(.08)
    io.sockets = [first, second]; io.addresses *= 2
    with pytest.raises(ChannelTimeout): dial(connect_timeout=.1)
    assert io.waits == pytest.approx([.02]) and first.closed and second.closed


@pytest.mark.parametrize('mode', ['pending', 'interrupted'])
def test_connect_is_not_restarted_and_success_restores_mode(io, mode):
    sock = FakeSocket(mode); io.sockets = [sock]
    io.steps = [(.25, False, lambda: None), (.25, False, lambda: None), (.01, True, lambda: None)]
    result = dial(timeout=7, connect_timeout=3)
    assert sock.connects == 1 and io.waits == [.25, .25, .25]
    assert sock.timeout == 3 and result.timeout == 7 and not sock.closed
    assert all(s.closed for s in io.selectors)
    result.close(); assert sock.closed


def test_so_error_refusal_closes_before_next_candidate(io):
    first, second = FakeSocket(error=errno.ECONNREFUSED), FakeSocket('success')
    io.sockets = [first, second]; io.addresses *= 2
    io.steps = [(.01, True, lambda: None)]
    result = dial(connect_timeout=None)
    assert first.closed and not second.closed and second.timeout is None
    assert first.connects == second.connects == 1
    result.close()


@pytest.mark.parametrize('failure', ['setblocking', 'setsockopt', 'settimeout', 'getsockopt', 'register', 'select'])
def test_socket_is_closed_on_setup_or_selector_errors(io, failure):
    sock = FakeSocket(); io.sockets = [sock]
    io.steps = [(.01, True, lambda: None)]
    if failure == 'register': io.register_failure = OSError('fixture registration failure')
    elif failure == 'select': io.select_failure = OSError('fixture selector failure')
    else: sock.fail = failure
    with pytest.raises(ChannelClosed): dial()
    assert sock.closed and all(s.closed for s in io.selectors)


def test_constructor_failure_does_not_transfer_socket_ownership(io):
    sock = FakeSocket('success'); io.sockets = [sock]
    with pytest.raises(TypeError): dial(unknown_argument=True)
    assert sock.closed


def test_cancel_after_immediate_connection_closes_before_options(io):
    cancel = threading.Event(); sock = FakeSocket('success'); io.sockets = [sock]
    sock.on_connect = cancel.set
    with pytest.raises(TaskCancelled): dial(cancel=cancel)
    assert sock.closed and not sock.options


@pytest.mark.parametrize('timeout', [-1, math.inf, math.nan])
def test_invalid_timeout_fails_before_resolution(io, timeout):
    with pytest.raises(ValueError): dial(connect_timeout=timeout)
    assert io.resolve_calls == 0


def test_empty_resolution_and_resolver_failure_are_transport_errors(io):
    io.addresses = []
    with pytest.raises(ChannelClosed, match='no addresses'): dial()
    def fail(): raise OSError('fixture resolver error')
    io.resolve_hook = fail
    with pytest.raises(ChannelClosed, match='fixture resolver error'): dial()
    assert not io.used


def test_unexpected_selector_error_also_closes_socket(io):
    sock = FakeSocket(); io.sockets = [sock]
    io.register_failure = RuntimeError('fixture selector implementation error')
    with pytest.raises(RuntimeError, match='implementation error'): dial()
    assert sock.closed and all(s.closed for s in io.selectors)


def test_cancellation_during_final_socket_setup_prevents_handoff(io):
    cancel = threading.Event(); sock = FakeSocket('success'); io.sockets = [sock]
    original = sock.settimeout
    def configure(value):
        original(value)
        cancel.set()
    sock.settimeout = configure
    with pytest.raises(TaskCancelled): dial(cancel=cancel)
    assert sock.closed
