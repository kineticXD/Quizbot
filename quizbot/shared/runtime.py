"""Compatibility-only runtime flags.

Creator functionality now runs on the same Bot API application as quiz
playback. There is no separate MTProto creator adapter in the live launcher.
The legacy flag remains available for older imports but is not used to gate
Bot API creator features or report service health.
"""

CREATOR_ADAPTER_READY: bool = False  # Deprecated; retained for compatibility only.
