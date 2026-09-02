from .base import TTSEngine
from .fish import FishReaderEngine
from .pocket import PocketTTSEngine


def load_engines() -> list[TTSEngine]:
    """All engines, available or not (the UI reports why one is disabled)."""
    return [PocketTTSEngine(), FishReaderEngine()]
