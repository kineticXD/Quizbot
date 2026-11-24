"""In-process health flags shared by the unified bot adapters."""

# False until the MTProto creator client has completed authorization.
CREATOR_ADAPTER_READY: bool = False
