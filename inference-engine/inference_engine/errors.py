class InferenceError(Exception):
    """Base class for inference-engine failures."""


class ConfigError(InferenceError):
    """The model configuration is invalid or inconsistent."""


class ShardError(InferenceError):
    """A shard request is outside the model, or its weights are unusable."""


from communication.errors import (ChannelClosed, ChannelTimeout, PeerError,  # noqa: E402
                                  TaskCancelled)
