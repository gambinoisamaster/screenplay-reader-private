from __future__ import annotations

from abc import ABC, abstractmethod

from pydub import AudioSegment


class TTSEngine(ABC):
    """A text-to-speech backend. Voices are addressed as 'EngineName: voice'."""

    name: str

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(usable, reason-if-not)."""

    @abstractmethod
    def voices(self) -> list[str]:
        pass

    @abstractmethod
    def synthesize(self, voice: str, text: str) -> AudioSegment:
        """Generate speech for one chunk of text."""
