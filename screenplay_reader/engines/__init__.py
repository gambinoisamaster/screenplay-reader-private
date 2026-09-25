from .base import TTSEngine
from .mlx_audio import OmniVoiceTTSEngine
from .pocket import PocketTTSEngine


def load_engines() -> list[TTSEngine]:
    """All engines, available or not (the UI reports why one is disabled)."""
    return [PocketTTSEngine(), OmniVoiceTTSEngine()]
