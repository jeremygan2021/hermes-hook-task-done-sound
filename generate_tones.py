"""Generate the three built-in tones: start.wav, success.wav, error.wav.

Run once on install: `python3 generate_tones.py`.
Idempotent — safe to re-run; overwrites in place.

Tones (22050 Hz, 16-bit mono):
  start.wav   — four short 880 Hz beeps (50 ms on / 90 ms off)  "滴滴滴滴"
  success.wav — two-note ascending: 660 Hz (120 ms) → 880 Hz (180 ms)  "当当"
  error.wav   — single low 220 Hz pulse (250 ms) for users who *want* failure audio
                (defaults.json has silent-on-failure so this file is shipped but unused)

PCM synthesis uses only the stdlib (wave, struct, math) — no numpy, no ffmpeg.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
SR = 22050


def _envelope(i: int, n: int) -> float:
    """5 ms attack / 20 ms release to suppress click artifacts."""
    a = int(SR * 0.005)
    r = int(SR * 0.020)
    if i < a:
        return i / a
    if i > n - r:
        return max(0.0, (n - i) / r)
    return 1.0


def _tone(freq: float, dur_ms: int, vol: float = 0.5) -> list[int]:
    n = int(SR * dur_ms / 1000)
    return [
        int(math.sin(2 * math.pi * freq * i / SR) * vol * _envelope(i, n) * 32767)
        for i in range(n)
    ]


def _silence(ms: int) -> list[int]:
    return [0] * int(SR * ms / 1000)


def _write(name: str, samples: list[int]) -> Path:
    p = HERE / name
    with wave.open(str(p), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    return p


def build_start() -> Path:
    """滴滴滴滴 — four 70 ms beeps at 880 Hz, 90 ms gaps."""
    out: list[int] = []
    for _ in range(4):
        out += _tone(880, 70, vol=0.45)
        out += _silence(90)
    return _write("start.wav", out)


def build_success() -> Path:
    """失落 — descending three-note: 880 → 660 → 440 Hz, longer gaps, fading volume.

    Conveys "task is done but something feels off / underwhelming".
    Each tone is longer than the previous one, with a longer silence after,
    and volume decays across the three notes.
    """
    notes = [
        (880, 110, 0.55,  70),   # freq, dur_ms, vol, gap_ms
        (660, 170, 0.45, 110),
        (440, 260, 0.32,   0),    # last note — no trailing gap
    ]
    out: list[int] = []
    for freq, dur, vol, gap in notes:
        out += _tone(freq, dur, vol=vol)
        out += _silence(gap)
    return _write("success.wav", out)


def build_error() -> Path:
    """Single low pulse — 220 Hz, 280 ms. Shipped but silent by default."""
    return _write("error.wav", _tone(220, 280, vol=0.6))


if __name__ == "__main__":
    p1, p2, p3 = build_start(), build_success(), build_error()
    print(f"  wrote {p1.name}  ({p1.stat().st_size:>6} bytes)")
    print(f"  wrote {p2.name}  ({p2.stat().st_size:>6} bytes)")
    print(f"  wrote {p3.name}  ({p3.stat().st_size:>6} bytes)")
