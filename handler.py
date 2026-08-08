"""
task-done-sound — audio notification hook for Hermes Agent.

Fires on agent:start / agent:end:
  - start   → "滴滴" (four short 880 Hz beeps)
  - success → "当当" (two-note ascending: 660 Hz → 880 Hz)
  - failure → SILENT by default (set `failure_sound` in defaults.json to enable)

Config: ~/.hermes/hooks/task_done_sound/defaults.json
  {
    "mode": "sound",                         # "sound" | "tts" | "both"
    "start_sound":   "<absolute path>",      # default: <hook-dir>/start.wav
    "success_sound": "<absolute path>",      # default: <hook-dir>/success.wav
    "failure_sound": "<absolute path|null>", # default: null (silent on failure)
    "failure_patterns": ["⚠️", "❌", "failed", "encountered an error",
                         "traceback", "fatal:"],
    "volume": 0.6,                           # 0.0–1.0 (paplay --volume)
    "player": "auto",                        # paplay | aplay | mpv | ffplay | auto
    "delay_seconds": 3                       # defer non-CLI platforms so the
                                             # notification lands AFTER the body
  }

All playback is fire-and-forget in a background thread — the hook returns
immediately and never blocks the agent. Any exception is caught and printed
to stderr, never crashing the agent.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
CONFIG_PATH = HOOK_DIR / "defaults.json"

DEFAULT_CONFIG = {
    "mode": "sound",
    "start_sound": str(HOOK_DIR / "start.wav"),
    "success_sound": str(HOOK_DIR / "success.wav"),
    "failure_sound": None,                   # null = silent on failure
    "failure_patterns": [
        "⚠️", "❌", "failed", "encountered an error",
        "traceback", "fatal:", "panic:",
    ],
    "volume": 0.6,
    "player": "auto",
    "delay_seconds": 3,
}


def _load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.is_file():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception as e:                # noqa: BLE001
            print(f"[task-done-sound] bad {CONFIG_PATH}: {e}", file=sys.stderr)
    return cfg


def _pick_player(requested: str) -> str | None:
    table = {
        "auto":   ["paplay", "aplay", "mpv", "ffplay"],
        "paplay": ["paplay"],
        "aplay":  ["aplay"],
        "mpv":    ["mpv"],
        "ffplay": ["ffplay"],
    }
    for cand in table.get(requested, [requested]):
        if shutil.which(cand):
            return cand
    return None


def _looks_like_failure(text: str, patterns: list[str]) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(p.lower() in lower for p in patterns)


def _play(sound_path: str | None, volume: float, player: str) -> None:
    if not sound_path:
        return                                # failure_sound=null → silent
    p = Path(sound_path)
    if not p.is_file():
        print(f"[task-done-sound] sound not found: {sound_path}", file=sys.stderr)
        return
    v = max(0.0, min(1.0, float(volume)))
    try:
        if player == "paplay":
            cmd = ["paplay", f"--volume={int(v * 65535)}", str(p)]
        elif player == "aplay":
            cmd = ["aplay", "-q", str(p)]
        elif player == "mpv":
            cmd = ["mpv", "--no-terminal", "--really-quiet", f"--volume={int(v * 100)}", str(p)]
        elif player == "ffplay":
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                   "-volume", str(int(v * 100)), str(p)]
        else:
            return
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:                    # noqa: BLE001
        print(f"[task-done-sound] playback failed: {e}", file=sys.stderr)


def _tts_say(text: str) -> None:
    """Best-effort TTS — tries edge-tts (already a Hermes dep), then espeak."""
    text = text.strip()
    if not text:
        return
    # 1. edge-tts
    try:
        import edge_tts  # type: ignore
        async def _run() -> None:
            comm = edge_tts.Communicate(text, voice="zh-CN-XiaoxiaoNeural")
            await comm.save("/tmp/task-done-sound-last.mp3")
        asyncio.run(_run())
        subprocess.Popen(
            ["mpv", "--no-terminal", "--really-quiet", "/tmp/task-done-sound-last.mp3"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return
    except Exception:
        pass
    # 2. espeak
    if shutil.which("espeak"):
        subprocess.Popen(["espeak", "-v", "zh", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _fire(cfg: dict, kind: str, response: str) -> None:
    """kind ∈ {'start', 'success', 'failure'}."""
    if kind == "start":
        sound_path = cfg["start_sound"]
    elif kind == "success":
        sound_path = cfg["success_sound"]
    else:                                    # failure
        sound_path = cfg.get("failure_sound")
    player = _pick_player(cfg.get("player", "auto"))
    if player is None:
        print("[task-done-sound] no audio player found "
              "(install paplay / aplay / mpv / ffplay)", file=sys.stderr)
        return
    volume = cfg.get("volume", 0.6)
    mode = cfg.get("mode", "sound")
    summary = response.strip().splitlines()[0][:240] if response else ""

    def _runner() -> None:
        if mode in ("sound", "both"):
            _play(sound_path, volume, player)
        if mode in ("tts", "both") and summary:
            if kind == "start":
                _tts_say("收到")
            elif kind == "success":
                _tts_say("完成")
            else:
                _tts_say("失败")

    threading.Thread(target=_runner, daemon=True).start()


def handle(event_type, context):
    cfg = _load_config()
    response = (context or {}).get("response", "") or ""
    platform = (context or {}).get("platform", "")
    delay = 0 if platform in ("cli", "tui") else float(cfg.get("delay_seconds", 3))

    try:
        if event_type == "agent:start":
            _fire(cfg, "start", "")
            return

        if event_type != "agent:end":
            return

        failed = _looks_like_failure(response, cfg.get("failure_patterns", []))
        kind = "failure" if failed else "success"

        if delay > 0:
            def _delayed():
                import time
                time.sleep(delay)
                _fire(cfg, kind, response)
            threading.Thread(target=_delayed, daemon=True).start()
        else:
            _fire(cfg, kind, response)
    except Exception as e:                    # noqa: BLE001
        print(f"[task-done-sound] handler error: {e}", file=sys.stderr)
