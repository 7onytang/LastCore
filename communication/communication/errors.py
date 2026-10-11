class TransportError(Exception):
    """Base class for every transport failure."""


class ProtocolError(TransportError):
    """A peer sent a frame that violates the protocol."""


class ChannelTimeout(TransportError):
    """A frame did not arrive before the socket timeout or task deadline."""


class ChannelClosed(TransportError):
    """The peer closed the connection before the exchange finished."""


class PeerError(TransportError):
    """The remote stage reported an execution failure."""

    def __init__(self, message, code="remote_error"):
        super().__init__(message)
        self.code = code


class TaskCancelled(TransportError):
    """The caller, an upstream peer, or the operator cancelled the task."""
