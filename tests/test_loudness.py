"""Loudness leveling: every line comes out at the same perceived volume."""

import math

import numpy as np
from pydub import AudioSegment

from screenplay_reader.loudness import (
    MAX_GAIN_DB,
    PEAK_CEILING_DBFS,
    TARGET_LUFS,
    _moving_mean,
    _moving_min,
    level,
    loudness,
)

RATE = 44100


def tone(amplitude: float, seconds: float = 2.0, freq: float = 997.0) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return amplitude * np.sin(2 * np.pi * freq * t)


def segment(samples: np.ndarray) -> AudioSegment:
    data = np.round(np.clip(samples, -1, 32767 / 32768) * 32768).astype(np.int16)
    return AudioSegment(data.tobytes(), frame_rate=RATE, sample_width=2, channels=1)


def measured(seg: AudioSegment) -> float:
    return loudness(np.frombuffer(seg.raw_data, np.int16) / 32768, seg.frame_rate)


def peak_dbfs(seg: AudioSegment) -> float:
    return 20 * math.log10(np.abs(np.frombuffer(seg.raw_data, np.int16)).max() / 32768)


def test_meter_matches_the_bs1770_calibration_tone():
    # The standard's reference: a full-scale 997 Hz sine in one channel reads -3.01 LUFS.
    assert abs(loudness(tone(1.0), RATE) - -3.01) < 0.05
    assert abs(loudness(tone(0.1), RATE) - -23.01) < 0.05


def test_quiet_and_loud_voices_come_out_equally_loud():
    for amplitude in (0.02, 0.05, 0.5, 1.0):  # -37 to -3 LUFS: up to 18 dB of gain either way
        assert abs(measured(level(segment(tone(amplitude)))) - TARGET_LUFS) < 0.2


def test_short_word_is_leveled_too():
    word = segment(tone(0.01, seconds=0.25))  # shorter than one 400 ms block
    assert abs(measured(level(word)) - TARGET_LUFS) < 0.2


def test_peaks_held_under_the_ceiling_without_losing_loudness():
    # Quiet speech with sharp transients: reaching the target means limiting the spikes.
    samples = tone(0.02, seconds=3.0, freq=200)
    samples[RATE::RATE // 4] = 0.6
    out = level(segment(samples))
    assert peak_dbfs(out) <= PEAK_CEILING_DBFS
    assert abs(measured(out) - TARGET_LUFS) < 0.2


def test_silence_is_left_alone():
    silent = AudioSegment.silent(duration=500, frame_rate=RATE)
    assert level(silent).raw_data == silent.raw_data


def test_very_quiet_recording_still_reaches_the_target():
    whisper = segment(tone(10 ** (-50 / 20)))  # about -53 LUFS: needs +34 dB
    assert abs(measured(level(whisper)) - TARGET_LUFS) < 0.2


def test_nearly_silent_take_is_not_blown_up_into_hiss():
    faint = segment(tone(10 ** (-62 / 20)))  # about -65 LUFS: would need +46 dB
    gained = measured(level(faint)) - measured(faint)
    assert abs(gained - MAX_GAIN_DB) < 0.2


def test_limiter_never_turns_a_peak_down_less_than_needed():
    # Including peaks on the very first and last samples, where the smoothing runs out of room.
    rng = np.random.default_rng(0)
    for n, half in ((50, 7), (1000, 44), (5000, 441)):
        needed = np.ones(n)
        spikes = np.concatenate(([0, n - 1], rng.integers(0, n, 8)))
        needed[spikes] = rng.uniform(0.05, 0.95, len(spikes))
        gain = _moving_mean(_moving_min(needed, half), half // 2)
        assert (gain <= needed + 1e-12).all()


def test_moving_windows_match_brute_force():
    rng = np.random.default_rng(1)
    for n, half in ((1, 3), (9, 4), (200, 13)):
        x = rng.uniform(0, 1, n)
        ones = np.concatenate((np.ones(half), x, np.ones(half)))
        edges = np.pad(x, half, mode="edge")
        assert np.allclose(_moving_min(x, half), [ones[i : i + 2 * half + 1].min() for i in range(n)])
        assert np.allclose(_moving_mean(x, half), [edges[i : i + 2 * half + 1].mean() for i in range(n)])


def test_length_and_format_unchanged():
    seg = segment(tone(0.3, seconds=1.234))
    out = level(seg)
    assert (len(out.raw_data), out.frame_rate, out.sample_width, out.channels) == (
        len(seg.raw_data), RATE, 2, 1)
