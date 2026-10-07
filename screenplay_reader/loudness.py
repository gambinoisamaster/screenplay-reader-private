"""Even out the volume of every generated line.

A cloning TTS engine copies the loudness of the sample it clones, so a quietly
recorded voice gives a quiet character and a hot recording a loud one. Each
spoken segment is measured for *perceived* loudness (ITU-R BS.1770, the LUFS
measure streaming services use) and turned up or down to one target, so every
voice comes out equally loud whatever level its sample was recorded at. Nobody
has to normalize a .wav before dropping it into assets/voices/.
"""

from __future__ import annotations

import math

import numpy as np
from pydub import AudioSegment

TARGET_LUFS = -19.0  # spoken-word level: audiobooks and podcasts sit around -16 to -20
PEAK_CEILING_DBFS = -1.0  # no sample ever goes past this, so nothing clips
# Enough for a line at -59 LUFS (a quiet sample can give lines well below its own level);
# anything quieter is a near-silent take, left quiet instead of blown up into hiss.
MAX_GAIN_DB = 40.0
_LIMIT_HALF_S = 0.01  # the limiter eases in and out over ~10 ms around a peak

_BLOCK_S = 0.4  # BS.1770 measures 400 ms blocks...
_HOP_S = 0.1  # ...overlapping by 75%
_ABSOLUTE_GATE = -70.0  # blocks quieter than this are silence
_RELATIVE_GATE = 10.0  # blocks this far below the average are pauses, not speech

# BS.1770 "K-weighting" (48 kHz coefficients): a +4 dB high shelf above ~1.5 kHz, the way
# the head boosts what reaches the ear, then a high-pass at ~38 Hz. As (b, a) biquads.
_SHELF = ((1.53512485958697, -2.69169618940638, 1.19839281085285), (1.0, -1.69065929318241, 0.73248077421585))
_HIGHPASS = ((1.0, -2.0, 1.0), (1.0, -1.99004745483398, 0.99007225036621))


def _k_weighting(freqs: np.ndarray) -> np.ndarray:
    """Magnitude response of the K-weighting filters at these frequencies (Hz, up to 24 kHz)."""
    z = np.exp(-2j * np.pi * freqs / 48000)  # z^-1 on the 48 kHz unit circle
    response = np.ones_like(z)
    for b, a in (_SHELF, _HIGHPASS):
        response *= (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    return np.abs(response)


def loudness(samples: np.ndarray, rate: int) -> float:
    """Integrated loudness, in LUFS, of mono samples scaled to -1..1 (rate up to 48 kHz).

    Returns -inf for silence. Segments shorter than one 400 ms block (a single
    word, a character's name) are measured as one block."""
    n = len(samples)
    if n == 0:
        return -math.inf
    # K-weight in the frequency domain (zero-padded, so no wrap-around); only the
    # energy of each block matters, and that depends on the magnitude response alone.
    size = 1 << (2 * n - 1).bit_length()
    spectrum = np.fft.rfft(samples, size) * _k_weighting(np.fft.rfftfreq(size, 1 / rate))
    weighted = np.fft.irfft(spectrum, size)[:n]

    block, hop = int(_BLOCK_S * rate), int(_HOP_S * rate)
    energy = np.concatenate(([0.0], np.cumsum(weighted * weighted)))
    if n <= block:
        mean_square = np.array([energy[-1] / n])
    else:
        starts = np.arange(0, n - block + 1, hop)
        mean_square = (energy[starts + block] - energy[starts]) / block

    with np.errstate(divide="ignore"):
        block_lufs = -0.691 + 10 * np.log10(mean_square)
    speech = block_lufs > _ABSOLUTE_GATE
    if not speech.any():
        return -math.inf
    gate = -0.691 + 10 * math.log10(mean_square[speech].mean()) - _RELATIVE_GATE
    speech &= block_lufs > gate
    return -0.691 + 10 * math.log10(mean_square[speech].mean())


def _moving_min(x: np.ndarray, half: int) -> np.ndarray:
    """Minimum of x over [i - half, i + half] for every i (van Herk / Gil-Werman, O(n))."""
    size = 2 * half + 1
    tail = half + (-(len(x) + 2 * half)) % size  # pad to whole blocks; 1 = no gain reduction
    blocks = np.concatenate((np.ones(half), x, np.ones(tail))).reshape(-1, size)
    prefix = np.minimum.accumulate(blocks, axis=1).ravel()
    suffix = np.minimum.accumulate(blocks[:, ::-1], axis=1)[:, ::-1].ravel()
    i = np.arange(len(x))
    return np.minimum(suffix[i], prefix[i + size - 1])


def _moving_mean(x: np.ndarray, half: int) -> np.ndarray:
    """Mean of x over [i - half, i + half] for every i (edges padded with the edge values,
    so a dip at the very first or last sample is kept, not averaged away)."""
    size = 2 * half + 1
    total = np.concatenate(([0.0], np.cumsum(np.pad(x, half, mode="edge"))))
    return (total[size:] - total[:-size]) / size


def _limit(y: np.ndarray, ceiling: float, rate: int) -> np.ndarray:
    """Turn down only the moments that would pass ceiling, easing in before each peak and
    out after it, so the rest of the line keeps its full level."""
    over = np.abs(y) > ceiling
    if not over.any():
        return y
    needed = np.ones_like(y)
    needed[over] = ceiling / np.abs(y[over])
    half = max(1, int(_LIMIT_HALF_S * rate))
    # Spreading each dip over +/-half, then smoothing over +/-half/2, keeps the full dip
    # at the peak itself (every smoothed sample there averages values <= the dip).
    gain = _moving_mean(_moving_min(needed, half), half // 2)
    return np.clip(y * gain, -ceiling, ceiling)


def level(seg: AudioSegment, target_lufs: float = TARGET_LUFS) -> AudioSegment:
    """seg turned up or down to target_lufs, with peaks held under PEAK_CEILING_DBFS.

    Works in 16-bit mono at up to 48 kHz (audio_builder's working format)."""
    seg = seg.set_channels(1).set_sample_width(2)
    if seg.frame_rate > 48000:
        seg = seg.set_frame_rate(48000)
    samples = np.frombuffer(seg.raw_data, dtype=np.int16).astype(np.float64) / 32768
    current = loudness(samples, seg.frame_rate)
    if current == -math.inf:
        return seg
    ceiling = math.floor(10 ** (PEAK_CEILING_DBFS / 20) * 32768) / 32768  # on an int16 step
    gain_db = min(target_lufs - current, MAX_GAIN_DB)
    for _ in range(2):  # holding peaks down costs a little loudness; a second pass makes it up
        leveled = _limit(samples * 10 ** (gain_db / 20), ceiling, seg.frame_rate)
        short = target_lufs - loudness(leveled, seg.frame_rate)
        if short < 0.1 or gain_db >= MAX_GAIN_DB:
            break
        gain_db = min(gain_db + short, MAX_GAIN_DB)
    scaled = np.round(leveled * 32768)
    return seg._spawn(np.clip(scaled, -32768, 32767).astype(np.int16).tobytes())
