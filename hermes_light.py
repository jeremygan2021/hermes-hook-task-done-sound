"""
hermes-light — always-on-top GTK widget showing one status column per running
AI coding agent session (Hermes / Claude Code / OpenCode).

Each column:
  - an agent badge (distinct icon + brand color) at the top
  - three stacked status lights (red / yellow / green) below

State → lights:
  busy       → BLUE light, vertical bounce
  needs_perm → YELLOW solid
  success    → GREEN solid (click / 'a' acks → idle)
  failure    → RED blink
  idle       → all dim

Agent detection: scans `ps` for comm names `hermes`, `claude`, `opencode`
(plus node/bun argv0 fallbacks) and builds one column per session.

Communication: Unix datagram socket at ~/.hermes/run/hermes-light.sock.
  Hook/watch → GUI:  {"session": "pts/0:54990", "state": "busy"}
  GUI → hook/watch:  {"ack": true, "session": "pts/0:54990"}

Drag the window anywhere with the left button; click (no drag) on a green
column to ack; 'a' acks the first success column.

Run:
  hermes-light             # foreground, Ctrl-C to stop
  hermes-light --bg        # background (pidfile-guarded)
  hermes-light --stop      # stop the background instance
"""
from __future__ import annotations

import argparse
import cairo
import gi
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

SOCKET_PATH = os.path.expanduser("~/.hermes/run/hermes-light.sock")
PIDFILE = os.path.expanduser("~/.hermes/run/hermes-light.pid")
LOGFILE = os.path.expanduser("~/.hermes/logs/hermes-light.log")
SETTINGS_PATH = os.path.expanduser("~/.hermes/hermes-light-settings.json")
TTS_WAV_DIR = os.path.expanduser("~/.local/share/hermes-light/tts/wav")

# ──── visual constants ───────────────────────────────────────────
COL_W = 82                  # column width (compact, modern)
COL_H = 152                 # column height
COL_GAP = 6
BADGE_R = 19                 # badge radius
BADGE_Y = 60                 # badge center y
LIGHT_R = 5                  # small inline lights
LIGHT_GAP = 5
PANEL_RADIUS = 14            # rounded corners (per-column card)
PANEL_PAD = 8                # inner padding
CHASE_PERIOD_MS = 420        # one full chaser cycle
BOUNCE_AMPL = 6

# Inline 3-light positions at top of column
LIGHTS_Y = 18                # y of all 3 lights
LIGHTS_X_STEP = LIGHT_R * 2 + LIGHT_GAP  # step between lights
GEAR_INSET = 18              # gear / indicator offset from panel right edge

# Status light colors
STATUS_COLORS = {
    "blue":   (0.28, 0.60, 1.00),
    "yellow": (1.00, 0.82, 0.18),
    "green":  (0.34, 0.85, 0.46),
    "red":    (1.00, 0.34, 0.36),
    "dim":    (0.16, 0.16, 0.19),
}

# Agent identity — brand color + SVG logo
LOGO_DIR = os.path.expanduser("~/.local/share/hermes-light/logos")

AGENTS = {
    "hermes":   {"color": (0.55, 0.40, 0.95),  "svg": "hermes.svg"},
    "claude":   {"color": (0.95, 0.62, 0.34),  "svg": "claude.svg"},
    "opencode": {"color": (0.20, 0.80, 0.70),  "svg": "opencode.svg"},
}

_rsvg_cache: dict[str, cairo.Surface] = {}


# Brand colors used to recolor `currentColor` SVG logos (hex, no white bg)
LOGO_FILL = {
    "hermes":   "#8b5cf6",   # violet — Hermes brand
    "claude":   "#d97757",   # clay/orange — Claude brand
    "opencode": "#2dd4bf",   # teal — OpenCode brand
}

_LOGO_FILL_CSS = {
    agent: f"*{{fill:{color}!important;}}"
    for agent, color in LOGO_FILL.items()
}


def _load_logo_surface(agent: str) -> cairo.Surface | None:
    """Render the agent's SVG logo at 64x64 on TRANSPARENT background.

    The SVGs use `fill="currentColor"` which librsvg renders as black by
    default. We inject a CSS rule before parsing that forces every element
    to the agent's brand color, so the logo renders in color with a
    transparent background — no white disc needed. The badge provides the
    brand-colored disc behind it.
    """
    if agent in _rsvg_cache:
        return _rsvg_cache[agent]
    path = os.path.join(LOGO_DIR, AGENTS[agent]["svg"])
    if not os.path.exists(path):
        return None
    try:
        gi.require_version("Rsvg", "2.0")
        from gi.repository import Rsvg  # noqa: F811
        raw = Path(path).read_text()
        # Inject CSS forcing brand color; librsvg supports <style> in data.
        css = _LOGO_FILL_CSS.get(agent, "")
        svg_with_css = raw.replace(
            "<svg",
            f'<svg><style>{css}</style>',
            1,
        ) if css else raw
        handle = Rsvg.Handle.new_from_data(svg_with_css.encode())
        size = 64
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        ctx = cairo.Context(surf)
        # Transparent background — logo draws in its brand color
        ctx.scale(size / 24.0, size / 24.0)
        handle.render_cairo(ctx)
        _rsvg_cache[agent] = surf
        return surf
    except Exception:
        return None


# ──── session registry ───────────────────────────────────────────
SUCCESS_AUTOFADE_SECS = 8.0         # auto-clear green light after this many seconds


# ──── settings (persisted to JSON, shared with hermes-watch) ───────
DEFAULT_SETTINGS = {
    "sound_enabled":   True,        # play the success/start wav at all
    "tts_enabled":     True,        # also speak a Chinese report on done/needs_perm
    "tts_voice":       "zh-CN-XiaoxiaoNeural",
    "tts_volume":      0.7,         # 0.0–1.0, applied to paplay
    "tts_player":      "paplay",    # player used for TTS (defaults to wav-compatible)
}

# Voices we expose in the dropdown (Chinese + a couple of English fallbacks)
TTS_VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
    "en-US-AriaNeural",
    "en-US-GuyNeural",
]

# TTS report files: key → relative filename in TTS_WAV_DIR
TTS_REPORTS = {
    "task_done":    "task_done.wav",
    "hermes_done":  "hermes_done.wav",
    "claude_done":  "claude_done.wav",
    "opencode_done": "opencode_done.wav",
    "needs_perm":   "needs_perm.wav",
}


def _load_settings() -> dict:
    """Load settings from disk; return DEFAULT_SETTINGS on any failure."""
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
        return merged
    except (OSError, ValueError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def _save_settings(s: dict) -> None:
    """Persist settings to disk; best-effort."""
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except OSError as e:
        _log(f"settings save failed: {e}")


settings_lock = threading.Lock()
settings: dict = _load_settings()


def get_settings() -> dict:
    with settings_lock:
        return dict(settings)


def update_settings(**changes) -> dict:
    """Update one or more settings, persist, and return the new dict."""
    with settings_lock:
        for k, v in changes.items():
            if k in DEFAULT_SETTINGS:
                settings[k] = v
        _save_settings(settings)
        return dict(settings)


class Session:
    __slots__ = ("key", "label", "agent", "state", "success_since", "model", "start_ts", "pid")

    def __init__(self, key: str, label: str, agent: str = "hermes", pid: int = 0):
        self.key = key
        self.label = label
        self.agent = agent if agent in AGENTS else "hermes"
        self.state = "idle"
        self.success_since: float = 0.0   # epoch time when state entered "success"
        self.model: str = ""              # LLM model in use (from state.db)
        self.start_ts: float = 0.0        # process start epoch (for uptime)
        self.pid = pid                    # process id (0 = unknown)


sessions_lock = threading.Lock()
sessions: dict[str, Session] = {}
sessions_order: list[str] = []
_last_busy_push: dict[str, float] = {}   # key → last time watch pushed "busy"


def _sentinel_busy(pid: int) -> bool:
    """True if /tmp/hermes-status-<pid> exists (wrapper wrote it on startup)."""
    return os.path.exists(f"/tmp/hermes-status-{pid}")


_HOOK_STALE_SECS = 60.0


def _hook_state(pid: int) -> str | None:
    """Read Hermes' official lifecycle hook state for a pid.

    Returns "busy", "needs_perm", or None if no fresh state file.
    """
    try:
        p = f"/tmp/hermes-hook-{pid}.state"
        if not os.path.exists(p):
            return None
        with open(p) as f:
            parts = f.read().strip().split()
        if len(parts) < 2:
            return None
        state, ts = parts[0], float(parts[1])
        if time.time() - ts > _HOOK_STALE_SECS:
            return None
        return state
    except (OSError, ValueError, IndexError):
        return None


def add_or_update(key: str, label: str, agent: str = "hermes", pid: int = 0) -> None:
    with sessions_lock:
        s = sessions.get(key)
        if s is None:
            s = Session(key, label, agent, pid)
            sessions[key] = s
            sessions_order.append(key)
        else:
            s.label = label
            if agent in AGENTS:
                s.agent = agent
            if pid:
                s.pid = pid
        # Refresh model + uptime metadata from state.db / /proc
        if pid:
            s.model = model_for_pid(pid) or s.model
            s.start_ts = _proc_start_epoch(pid) or s.start_ts
        # Official hook state check: /tmp/hermes-hook-<pid>.state is the
        # AUTHORITATIVE signal (written by Hermes itself via shell hooks).
        # A recent socket push (success/failure/needs_perm) wins for 12s so
        # the green light stays visible, then autofade handles the rest.
        hstate = _hook_state(pid) if pid else None
        recent_push = time.time() - _last_busy_push.get(key, 0) <= 12.0
        if hstate == "needs_perm":
            s.state = "needs_perm"
            s.success_since = 0.0
        elif hstate == "busy":
            if not recent_push:
                if s.state != "busy":
                    s.state = "busy"
                    s.success_since = 0.0
        elif s.state == "needs_perm":
            # Hook file gone (approval answered) → back to busy.
            if not recent_push:
                s.state = "busy"
                s.success_since = 0.0
        elif recent_push:
            # Watch just told us the state (busy/success/needs_perm) — respect it.
            pass
        elif s.state == "busy":
            # No fresh push, no hook, no sentinel → fall back to idle.
            s.state = "idle"


def set_state(key: str, state: str) -> bool:
    if state not in ("busy", "needs_perm", "success", "failure", "idle"):
        return False
    with sessions_lock:
        s = sessions.get(key)
        if s is None:
            s = Session(key, key)
            sessions[key] = s
            sessions_order.append(key)
        s.state = state
        if state == "success":
            s.success_since = time.time()
        else:
            s.success_since = 0.0
        if state == "busy":
            _last_busy_push[key] = time.time()
        elif state in ("success", "failure", "needs_perm"):
            _last_busy_push[key] = time.time()   # recent push → don't override w/ sentinel
        return True


def autofade_success() -> None:
    """Background tick: clear green light after SUCCESS_AUTOFADE_SECS of solitude.

    If the agent restarts within the window (state moves to busy), the timer
    resets. This is the safety net for the rare case where hermes-watch misses
    a "done" beat — the green light still doesn't get stuck forever.
    """
    cutoff = time.time() - SUCCESS_AUTOFADE_SECS
    with sessions_lock:
        for s in sessions.values():
            if s.state == "success" and s.success_since > 0 and s.success_since < cutoff:
                _log(f"autofade: {s.key} success → idle ({SUCCESS_AUTOFADE_SECS}s elapsed)")
                s.state = "idle"
                s.success_since = 0.0


def drop_if_missing(alive: set[str]) -> None:
    with sessions_lock:
        for k in list(sessions_order):
            if k not in alive:
                sessions.pop(k, None)
                sessions_order.remove(k)


def snapshot() -> list[Session]:
    with sessions_lock:
        return [sessions[k] for k in sessions_order]


# ──── session-name resolution ────────────────────────────────────
_SESSION_DB = os.path.expanduser("~/.hermes/state.db")


def _proc_start_epoch(pid: int) -> float:
    """Process start time as epoch, from /proc (no locale-dependent ps)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        rp = s.rfind(")")
        start_ticks = int(s[rp + 1:].split()[19])
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime"):
                    boot = int(line.split()[1])
                    break
        return boot + start_ticks / 100.0
    except Exception:
        return 0.0


def session_label_for_pid(pid: int) -> str | None:
    """Map a hermes PID to its session's display name or short start time."""
    try:
        import sqlite3
        pep = _proc_start_epoch(pid)
        if pep <= 0:
            return None
        conn = sqlite3.connect(f"file:{_SESSION_DB}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT id, display_name, started_at, ended_at FROM sessions "
            "WHERE source='cli' ORDER BY started_at DESC LIMIT 30"
        ).fetchall()
        conn.close()
    except Exception:
        return None
    active = [r for r in rows if r[3] is None]
    if not active:
        return None
    best = min(active, key=lambda r: abs(r[2] - pep))
    sid, name, _ts, _ended = best
    # Brand-new session not yet written? Fall back to process start time.
    if abs(_ts - pep) > 300 and pep > _ts:
        return time.strftime("%H:%M", time.localtime(pep))
    if name:
        return name
    try:
        time_part = sid.split("_")[1]
        return f"{time_part[:2]}:{time_part[2:4]}"
    except (IndexError, ValueError):
        return sid[:12]


def model_for_pid(pid: int) -> str | None:
    """Look up the LLM model used by the given hermes session (from state.db).

    state.db.sessions has a `model` column; match by process start time.
    Tolerates up to 600s drift so a session that's a few minutes old still
    resolves; also tries argv session-id matching as a fallback.
    """
    try:
        import sqlite3
        pep = _proc_start_epoch(pid)
        if pep <= 0:
            return None
        conn = sqlite3.connect(f"file:{_SESSION_DB}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT id, model, started_at FROM sessions "
            "WHERE source='cli' AND model IS NOT NULL AND model != '' "
            "ORDER BY started_at DESC LIMIT 30"
        ).fetchall()
        conn.close()
    except Exception:
        return None
    if not rows:
        return None
    # 1) cmdline may carry the session id (hermes runs with session context)
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().decode("utf-8", "replace").split("\0")
        joined = " ".join(args)
        for sid, model, _ts in rows:
            if sid in joined:
                return model
    except OSError:
        pass
    # 2) start-time matching with 600s tolerance
    best = min(rows, key=lambda r: abs(r[2] - pep))
    if abs(best[2] - pep) > 600:
        return None
    return best[1]


# ──── agent discovery ────────────────────────────────────────────
def detect_agent(pid: int, comm: str) -> str:
    """Map a process to an agent type.  Checks comm first, then argv0.

    Python/node wrapper processes only count as an agent if their argv0 is
    literally the agent binary — a python3 process whose cmdline merely
    mentions "opencode" (e.g. our own test scripts) is NOT an agent.
    """
    c = comm.lower()
    if c == "hermes":
        return "hermes"
    if c in ("claude", "claude-code", "claude-code-cli"):
        return "claude"
    if c in ("opencode", "opencode-cli"):
        return "opencode"
    # node / bun shims: argv0 must be the agent binary itself
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().decode("utf-8", "replace").split("\0")
    except OSError:
        return ""
    argv0 = args[0].split("/")[-1].lower() if args else ""
    if argv0 in ("claude", "claude-code", "claude-code-cli"):
        return "claude"
    if argv0 in ("opencode", "opencode-cli", "opencode-tui"):
        return "opencode"
    return ""


def discover_sessions() -> list[tuple[str, str, str]]:
    """Return [(key, label, agent)] for running agent CLI processes.

    Keys match what the watcher/hook pushes: "<tty>:<pid>".
    """
    try:
        out = subprocess.check_output(["ps", "-eo", "pid=,comm="], text=True)
    except subprocess.CalledProcessError:
        return []
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        pid_s, comm = parts
        pid_i = int(pid_s)
        agent = detect_agent(pid_i, comm)
        if not agent:
            continue
        # Resolve the tty (ps tty column) for a stable label
        try:
            tty_out = subprocess.check_output(
                ["ps", "-p", pid_s, "-o", "tty="], text=True
            ).strip()
        except subprocess.CalledProcessError:
            tty_out = "?"
        # Prefer the session name/start-time when we can resolve it.
        sess_label = session_label_for_pid(pid_i)
        if sess_label:
            label = sess_label
        elif tty_out == "?":
            label = f"{agent} {pid_s}"
        else:
            label = tty_out
        if tty_out == "?":
            key = f"{agent}:{pid_s}"
        else:
            key = f"{tty_out}:{pid_s}"
        if key in seen:
            continue
        seen.add(key)
        found.append((key, label, agent, pid_i))
    return found


# ──── drawing helpers ────────────────────────────────────────────
def _rounded_rect(ctx: cairo.Context, x: float, y: float, w: float, h: float, r: float):
    ctx.move_to(x + r, y)
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def _light(ctx: cairo.Context, cx: float, cy: float, rgb, alpha: float, radius: float):
    """A status light: soft outer glow + radial-gradient core + specular."""
    # glow
    ctx.set_source_rgba(*rgb, alpha * 0.28)
    ctx.arc(cx, cy, radius + 5, 0, 2 * math.pi)
    ctx.fill()
    # core (radial gradient)
    grad = cairo.RadialGradient(cx - radius * 0.35, cy - radius * 0.35, radius * 0.2,
                                cx, cy, radius)
    grad.add_color_stop_rgba(0, min(1, rgb[0] + 0.25), min(1, rgb[1] + 0.25),
                             min(1, rgb[2] + 0.25), alpha)
    grad.add_color_stop_rgba(1, rgb[0], rgb[1], rgb[2], alpha)
    ctx.set_source(grad)
    ctx.arc(cx, cy, radius, 0, 2 * math.pi)
    ctx.fill()
    # specular dot
    ctx.set_source_rgba(1, 1, 1, alpha * 0.5)
    ctx.arc(cx - radius * 0.3, cy - radius * 0.35, radius * 0.28, 0, 2 * math.pi)
    ctx.fill()


def _badge(ctx: cairo.Context, cx: float, cy: float, agent: str):
    """Agent identity badge: brand-colored disc + colored SVG logo, no white bg."""
    rgb = AGENTS[agent]["color"]
    # outer glow (brand color, soft)
    ctx.set_source_rgba(*rgb, 0.40)
    ctx.arc(cx, cy, BADGE_R + 6, 0, 2 * math.pi)
    ctx.fill()

    # disc with radial gradient in brand color (darker edge → lighter center)
    grad = cairo.RadialGradient(cx - BADGE_R * 0.3, cy - BADGE_R * 0.3, BADGE_R * 0.15,
                                cx, cy, BADGE_R)
    grad.add_color_stop_rgba(0, min(1, rgb[0] + 0.35), min(1, rgb[1] + 0.35),
                             min(1, rgb[2] + 0.35), 1.0)
    grad.add_color_stop_rgba(1, rgb[0], rgb[1], rgb[2], 1.0)
    ctx.set_source(grad)
    ctx.arc(cx, cy, BADGE_R, 0, 2 * math.pi)
    ctx.fill()

    # subtle inner rim for depth
    ctx.set_line_width(1.2)
    ctx.set_source_rgba(1, 1, 1, 0.28)
    ctx.arc(cx, cy, BADGE_R - 1.5, 0, 2 * math.pi)
    ctx.stroke()

    # logo on top — transparent surface, brand-color logo, fills the disc
    surf = _load_logo_surface(agent)
    if surf is not None:
        sw, sh = surf.get_width(), surf.get_height()
        if sw > 0 and sh > 0:
            # Fill the disc diameter (slightly padded so logo doesn't clip)
            target_size = (BADGE_R - 1) * 2.0
            scale = target_size / sw
            ctx.save()
            ctx.translate(cx, cy)
            ctx.scale(scale, scale)
            ctx.translate(-sw / 2, -sh / 2)
            ctx.set_source_surface(surf, 0, 0)
            ctx.paint()
            ctx.restore()
    else:
        # fallback: agent letter
        label = agent[0].upper()
        ctx.set_source_rgba(1, 1, 1, 0.95)
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(BADGE_R * 1.1)
        xb, yb, w, h, _dx, _dy = ctx.text_extents(label)
        ctx.move_to(cx - w / 2 - xb, cy - h / 2 - yb)
        ctx.show_text(label)


# ──── drawing area ───────────────────────────────────────────────
class LightPanel(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_size_request(COL_W, COL_H)
        self.connect("draw", self._on_draw)

    def _on_draw(self, widget, ctx: cairo.Context):
        sess = snapshot()
        if not sess:
            return False

        n = len(sess)
        w = n * COL_W + (n - 1) * COL_GAP
        h = COL_H

        # 1. Outer drop shadow (subtle, behind panel)
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)

        # Drop shadow (multi-pass blur approximation)
        for off, alpha in [(4, 0.18), (2, 0.30), (1, 0.40)]:
            _rounded_rect(ctx, off, off + 2, w, h, PANEL_RADIUS)
            ctx.set_source_rgba(0, 0, 0, alpha)
            ctx.fill()

        # 2. Per-column card backgrounds (rounded, with subtle gradient)
        for i in range(n):
            cx_card = i * (COL_W + COL_GAP)
            # shadow
            _rounded_rect(ctx, cx_card + 1, 3, COL_W, h, PANEL_RADIUS)
            ctx.set_source_rgba(0, 0, 0, 0.5)
            ctx.fill()
            # card body
            _rounded_rect(ctx, cx_card, 0, COL_W, h, PANEL_RADIUS)
            grad = cairo.LinearGradient(0, 0, 0, h)
            grad.add_color_stop_rgba(0, 0.14, 0.15, 0.20, 0.92)
            grad.add_color_stop_rgba(1, 0.06, 0.06, 0.10, 0.94)
            ctx.set_source(grad)
            ctx.fill()
            # top inner highlight
            _rounded_rect(ctx, cx_card + 0.5, 0.5, COL_W - 1, h - 1, PANEL_RADIUS)
            ctx.set_source_rgba(1, 1, 1, 0.08)
            ctx.set_line_width(1.0)
            ctx.stroke()

        now = time.time()
        chase_phase = (now * 1000 / CHASE_PERIOD_MS) % 1.0
        blink = 0.45 + 0.55 * (0.5 + 0.5 * math.cos(now * 2 * math.pi))

        for i, s in enumerate(sess):
            cx = COL_W / 2 + i * (COL_W + COL_GAP)

            # 4a. Inline 3 lights at top (horizontal)
            if s.state == "busy":
                # chaser: 0 → 1 → 2 → 1 → 0, blue
                tri = 2.0 - abs(2.0 - (chase_phase * 4.0) % 4.0)
                cur = min(2, max(0, int(round(tri))))
                layout = [("dim", 0), ("dim", 1), ("dim", 2)]
                layout[cur] = ("blue", cur)
            elif s.state == "needs_perm":
                # RED BLINKING — awaiting user approval
                layout = [("red", 0), ("dim", 1), ("dim", 2)]
            elif s.state == "success":
                layout = [("dim", 0), ("dim", 1), ("green", 2)]
            elif s.state == "failure":
                layout = [("red", 0), ("dim", 1), ("dim", 2)]
            else:
                layout = [("dim", 0), ("dim", 1), ("dim", 2)]

            for color, idx in layout:
                lx = cx + (idx - 1) * LIGHTS_X_STEP
                rgb = STATUS_COLORS[color]
                alpha = 1.0
                if color == "dim":
                    alpha = 0.40
                elif s.state in ("failure", "needs_perm") and color == "red":
                    alpha = blink
                _light(ctx, lx, LIGHTS_Y, rgb, alpha, LIGHT_R)

            # 4b. Big badge
            _badge(ctx, cx, BADGE_Y, s.agent)

            # 4c. Model name below the badge (or agent name fallback)
            model_label = s.model or s.agent.upper()
            if len(model_label) > 10:
                model_label = model_label[:9] + "…"
            ctx.set_source_rgba(1, 1, 1, 0.85)
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                                 cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(8)
            xb, yb, tw, th, _dx, _dy = ctx.text_extents(model_label)
            ctx.move_to(cx - tw / 2 - xb, BADGE_Y + BADGE_R + 16 - yb)
            ctx.show_text(model_label)

            # 4d. Bottom line: state text (left) + uptime (right)
            state_text = {
                "busy":       "running",
                "needs_perm": "授权",
                "success":    "done",
                "failure":    "failed",
                "idle":       "idle",
            }.get(s.state, "idle")
            state_color = {
                "busy":       (0.55, 0.85, 1.00),
                "needs_perm": (1.00, 0.34, 0.36),
                "success":    (0.34, 0.85, 0.46),
                "failure":    (1.00, 0.34, 0.36),
                "idle":       (0.55, 0.55, 0.65),
            }.get(s.state, (0.55, 0.55, 0.65))

            # uptime
            uptime_text = ""
            if s.start_ts > 0:
                up = max(0, int(now - s.start_ts))
                if up < 3600:
                    uptime_text = f"{up // 60:02d}:{up % 60:02d}"
                else:
                    uptime_text = f"{up // 3600}:{(up % 3600) // 60:02d}"

            ctx.set_source_rgba(*state_color, 0.95)
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                                 cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(8)
            xb, yb, tw, th, _dx, _dy = ctx.text_extents(state_text)
            ctx.move_to(cx - tw / 2 - xb, h - PANEL_PAD - yb)
            ctx.show_text(state_text)

            if uptime_text:
                ctx.set_source_rgba(1, 1, 1, 0.45)
                ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                                     cairo.FONT_WEIGHT_NORMAL)
                ctx.set_font_size(7)
                xb, yb, tw, th, _dx, _dy = ctx.text_extents(uptime_text)
                ctx.move_to(cx - tw / 2 - xb, h - PANEL_PAD - 10 - yb)
                ctx.show_text(uptime_text)

        return False


# ──── window with drag ───────────────────────────────────────────
class LightWindow(Gtk.Window):
    def __init__(self):
        super().__init__()
        self.set_title("hermes-light")
        self.set_resizable(False)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_keep_above(True)
        self.set_app_paintable(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.panel = LightPanel()
        self.add(self.panel)
        self.panel.show()

        self._drag_start_xy: tuple[int, int] | None = None
        self._drag_start_win_xy: tuple[int, int] | None = None
        self._drag_active = False

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.BUTTON_MOTION_MASK
                        | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("button-press-event", self._on_btn_press)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("button-release-event", self._on_btn_release)
        self.connect("key-press-event", self._on_key)

    def _on_btn_press(self, widget, event):
        if event.button == 1:
            self._drag_start_xy = (event.x_root, event.y_root)
            x, y = self.get_position()
            self._drag_start_win_xy = (x, y)
            self._drag_active = False
            self.grab_add()
            return True
        if event.button == 3:
            self._ack_at(event.x)
            return True
        return False

    def _on_motion(self, widget, event):
        if self._drag_start_xy is None:
            return False
        sx, sy = self._drag_start_xy
        dx = event.x_root - sx
        dy = event.y_root - sy
        if not self._drag_active and (abs(dx) > 3 or abs(dy) > 3):
            self._drag_active = True
        if self._drag_active:
            wx, wy = self._drag_start_win_xy
            self.move(wx + int(dx), wy + int(dy))
            return True
        return False

    def _on_btn_release(self, widget, event):
        if event.button == 1 and self._drag_start_xy is not None:
            self.grab_remove()
            if not self._drag_active:
                self._ack_at(event.x)
            self._drag_start_xy = None
            self._drag_start_win_xy = None
            self._drag_active = False
            return True
        return False

    def _on_key(self, widget, event):
        if event.keyval == Gdk.KEY_a:
            with sessions_lock:
                for k in sessions_order:
                    if sessions[k].state == "success":
                        sessions[k].state = "idle"
                        _send_ack(k)
                        break
            return True
        return False

    def _column_at(self, x: float) -> str | None:
        with sessions_lock:
            order = list(sessions_order)
        if not order:
            return None
        idx = int(x // (COL_W + COL_GAP))
        return order[idx] if 0 <= idx < len(order) else None

    def _hit_test(self, x: float, y: float = 14) -> str | None:
        """Hit-test the click location. Returns the session key or None."""
        return self._column_at(x)

    def _ack_at(self, x: float):
        key = self._column_at(x)
        if key is None:
            return
        with sessions_lock:
            s = sessions.get(key)
            if s is None or s.state != "success":
                return
            s.state = "idle"
        _send_ack(key)


def _send_ack(key: str):
    if not os.path.exists(SOCKET_PATH):
        return
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.sendto(json.dumps({"ack": True, "session": key}).encode(), SOCKET_PATH)
        s.close()
    except OSError:
        pass


def refresh_window_size(win: LightWindow):
    with sessions_lock:
        n = len(sessions_order)
    n = max(1, n)
    w = n * COL_W + (n - 1) * COL_GAP
    win.set_default_size(w, COL_H)
    win.resize(w, COL_H)
    win.queue_resize()
    win.panel.set_size_request(w, COL_H)


# ──── socket server ──────────────────────────────────────────────
def socket_server(write_log):
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    if os.path.exists(SOCKET_PATH):
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o660)

    def _serve():
        while True:
            try:
                data, _ = srv.recvfrom(4096)
            except OSError:
                return
            try:
                msg = json.loads(data.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                write_log(f"bad json: {data!r}")
                continue
            key = msg.get("session") or msg.get("pty") or "default"
            if msg.get("ack"):
                write_log(f"ack from {key}")
                continue
            state = msg.get("state") or msg.get("kind")
            if state in ("busy", "needs_perm", "success", "failure", "idle"):
                GLib.idle_add(set_state, key, state)
                write_log(f"recv: {key} → {state}")

    threading.Thread(target=_serve, daemon=True).start()
    return srv


# ──── daemon helpers ─────────────────────────────────────────────
def daemonize():
    if os.path.exists(PIDFILE):
        try:
            old = int(open(PIDFILE).read().strip())
            os.kill(old, 0)
            print(f"already running pid={old}", file=sys.stderr)
            sys.exit(1)
        except (OSError, ValueError):
            pass
    os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
    rfd = os.open(LOGFILE, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(rfd, 1)
    os.dup2(rfd, 2)
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    open(PIDFILE, "w").write(str(os.getpid()))


def stop_daemon():
    if not os.path.exists(PIDFILE):
        print("not running")
        return
    pid = int(open(PIDFILE).read().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"sent SIGTERM to {pid}")
    except OSError as e:
        print(f"failed: {e}")


def _log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
        with open(LOGFILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ──── main ───────────────────────────────────────────────────────
# ──── settings window ────────────────────────────────────────────
_active_settings_window = None


def _open_settings_window(parent):
    """Open (or raise) the settings window. Singleton — one window at a time."""
    global _active_settings_window
    win = _active_settings_window
    if win is not None and win.get_visible():
        win.present()
        return
    if win is not None:
        _active_settings_window = None
    win = SettingsWindow(parent)
    win.connect("destroy", lambda *_: _on_settings_closed())
    _active_settings_window = win


def _on_settings_closed():
    global _active_settings_window
    _active_settings_window = None


def _play_wav_async(path):
    """Best-effort fire-and-forget playback of a WAV file via paplay."""
    def _run():
        vol = get_settings().get("tts_volume", 0.7)
        try:
            subprocess.Popen(
                ["paplay", f"--volume={int(vol * 65535)}", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass
    import threading
    threading.Thread(target=_run, daemon=True).start()


class SettingsWindow(Gtk.Window):
    """Popup with sound/TTS/audio controls. Opened by clicking the gear icon."""

    def __init__(self, parent):
        super().__init__()
        self.set_title("hermes-light · settings")
        self.set_resizable(False)
        self.set_decorated(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_modal(False)
        self.set_default_size(380, -1)

        px, py = parent.get_position()
        self.move(px + 80, py - 20)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(14)
        outer.set_margin_bottom(14)
        outer.set_margin_start(16)
        outer.set_margin_end(16)

        outer.pack_start(self._section_label("声音"), False, False, 0)
        self.chk_sound = Gtk.CheckButton(label="启用声音提示（start / done 当当）")
        self.chk_sound.set_active(get_settings().get("sound_enabled", True))
        self.chk_sound.connect("toggled", self._on_sound_toggled)
        outer.pack_start(self.chk_sound, False, False, 4)

        outer.pack_start(self._section_label("TTS 语音播报"), False, False, 8)
        self.chk_tts = Gtk.CheckButton(label="启用 TTS 中文播报（任务完成 / 需要授权）")
        self.chk_tts.set_active(get_settings().get("tts_enabled", True))
        self.chk_tts.connect("toggled", self._on_tts_toggled)
        outer.pack_start(self.chk_tts, False, False, 4)

        vbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(Gtk.Label(label="语音:"), False, False, 0)
        self.voice_combo = Gtk.ComboBoxText()
        current_voice = get_settings().get("tts_voice", "zh-CN-XiaoxiaoNeural")
        active_idx = 0
        for i, v in enumerate(TTS_VOICES):
            self.voice_combo.append_text(v)
            if v == current_voice:
                active_idx = i
        self.voice_combo.set_active(active_idx)
        self.voice_combo.connect("changed", self._on_voice_changed)
        vbox.pack_start(self.voice_combo, True, True, 0)
        outer.pack_start(vbox, False, False, 4)

        vbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(Gtk.Label(label="音量:"), False, False, 0)
        self.vol_adj = Gtk.Adjustment(
            value=get_settings().get("tts_volume", 0.7),
            lower=0.0, upper=1.0, step_increment=0.05, page_increment=0.1, page_size=0,
        )
        self.vol_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.vol_adj)
        self.vol_scale.set_digits(2)
        self.vol_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.vol_adj.connect("value-changed", self._on_volume_changed)
        vbox.pack_start(self.vol_scale, True, True, 0)
        outer.pack_start(vbox, False, False, 4)

        test_label = Gtk.Label(xalign=0)
        test_label.set_markup("<b>试听报告音：</b>")
        outer.pack_start(test_label, False, False, 6)

        grid = Gtk.Grid()
        grid.set_row_spacing(4)
        grid.set_column_spacing(6)
        test_reports = [
            ("Agent 任务完成", "task_done"),
            ("Hermes 完成", "hermes_done"),
            ("Claude 完成", "claude_done"),
            ("OpenCode 完成", "opencode_done"),
            ("需要授权", "needs_perm"),
        ]
        for i, (label, key) in enumerate(test_reports):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", self._on_test, key)
            grid.attach(btn, i % 3, i // 3, 1, 1)
        outer.pack_start(grid, False, False, 0)

        self.btn_regen = Gtk.Button(label="🔄 用当前语音重新生成所有报告音")
        self.btn_regen.connect("clicked", self._on_regen)
        outer.pack_start(self.btn_regen, False, False, 8)

        self.status_label = Gtk.Label(xalign=0)
        self.status_label.set_markup('<span foreground="#888888">就绪</span>')
        outer.pack_start(self.status_label, False, False, 6)

        close = Gtk.Button(label="关闭")
        close.connect("clicked", lambda _: self.destroy())
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        hbox.pack_end(close, False, False, 0)
        outer.pack_end(hbox, False, False, 0)

        self.add(outer)
        self.show_all()

    def _section_label(self, text):
        l = Gtk.Label(xalign=0)
        l.set_markup(f"<b>{text}</b>")
        l.set_margin_top(4)
        l.set_margin_bottom(2)
        return l

    def _on_sound_toggled(self, btn):
        update_settings(sound_enabled=btn.get_active())
        self._set_status(f"声音提示: {'开' if btn.get_active() else '关'}")

    def _on_tts_toggled(self, btn):
        update_settings(tts_enabled=btn.get_active())
        self._set_status(f"TTS 播报: {'开' if btn.get_active() else '关'}")

    def _on_voice_changed(self, combo):
        voice = combo.get_active_text()
        if voice:
            update_settings(tts_voice=voice)
            self._set_status(f"语音 = {voice}")

    def _on_volume_changed(self, adj):
        update_settings(tts_volume=adj.get_value())

    def _on_test(self, btn, report_key):
        wav = os.path.join(TTS_WAV_DIR, TTS_REPORTS.get(report_key, ""))
        if not os.path.exists(wav):
            self._set_status(f"❌ 缺失: {wav}")
            return
        _play_wav_async(wav)
        self._set_status(f"▶ 播放 {report_key}")

    def _on_regen(self, btn):
        btn.set_sensitive(False)
        self._set_status("重新生成中…")
        import threading
        def work():
            voice = get_settings().get("tts_voice", "zh-CN-XiaoxiaoNeural")
            texts = {
                "task_done":    "Agent 任务已完成",
                "hermes_done":  "Hermes 已完成",
                "claude_done":  "Claude 已完成",
                "opencode_done": "OpenCode 已完成",
                "needs_perm":   "Agent 需要授权",
            }
            for key, text in texts.items():
                tmp_mp3 = os.path.join(TTS_WAV_DIR, f".tmp_{key}.mp3")
                wav     = os.path.join(TTS_WAV_DIR, TTS_REPORTS[key])
                try:
                    subprocess.run(
                        ["edge-tts", "--voice", voice, "--text", text, "--write-media", tmp_mp3],
                        capture_output=True, check=True, timeout=20,
                    )
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", tmp_mp3, "-ar", "22050", "-ac", "1", wav + ".new"],
                        capture_output=True, check=True, timeout=20,
                    )
                    os.replace(wav + ".new", wav)
                except Exception as e:
                    GLib.idle_add(self._set_status, f"❌ {key} 失败: {e}")
                    GLib.idle_add(btn.set_sensitive, True)
                    return
                finally:
                    if os.path.exists(tmp_mp3):
                        os.remove(tmp_mp3)
            GLib.idle_add(self._set_status, f"✓ 5 个报告音已用 {voice} 重新生成")
            GLib.idle_add(btn.set_sensitive, True)
        threading.Thread(target=work, daemon=True).start()

    def _set_status(self, msg):
        self.status_label.set_markup(f'<span foreground="#a0d090">{msg}</span>')


class HermesTrayIcon:
    """System tray icon (GNOME top-bar notification area).

    Uses AyatanaAppIndicator3 — appears in the GNOME top bar via the
    ubuntu-appindicators extension. Click opens the settings window.
    The icon color reflects aggregate state across all sessions.
    """

    def __init__(self, parent_window):
        self.parent = parent_window
        self.indicator = None
        self._build_menu()
        self._init_indicator()

    def _build_menu(self):
        """Right-click menu shown in the top bar."""
        self.menu = Gtk.Menu()

        item = Gtk.MenuItem(label="hermes-light · Settings")
        item.connect("activate", lambda *_: _open_settings_window(self.parent))
        self.menu.append(item)

        item = Gtk.MenuItem(label="Quit")
        item.connect("activate", lambda *_: Gtk.main_quit())
        self.menu.append(item)

        self.menu.show_all()

    def _init_indicator(self):
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3  # noqa: F811

        # Generate a per-state icon as a temp PNG so we can update by file path
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "hermes-light",
            "hermes-light-idle",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self.menu)
        self.indicator.set_title("hermes-light")

        # Generate the initial icon
        self._refresh_icon("idle")

    def _refresh_icon(self, aggregate_state: str) -> None:
        """Regenerate the icon PNG for the given aggregate state and update."""
        # Map state → color
        colors = {
            "busy":       (0.40, 0.75, 1.00),
            "needs_perm": (1.00, 0.82, 0.18),
            "success":    (0.34, 0.85, 0.46),
            "failure":    (1.00, 0.34, 0.36),
            "idle":       (0.55, 0.55, 0.65),
        }
        color = colors.get(aggregate_state, colors["idle"])
        out_path = f"/tmp/hermes-light-{aggregate_state}.png"

        # Use cairo to render a 32x32 icon: dark circle with bright center dot
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 32, 32)
        ctx = cairo.Context(surf)
        # transparent background
        ctx.set_operator(cairo.OPERATOR_CLEAR)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)
        # dark outer ring
        ctx.set_source_rgba(0.10, 0.10, 0.14, 1.0)
        ctx.arc(16, 16, 14, 0, 2 * math.pi)
        ctx.fill()
        # bright center
        ctx.set_source_rgba(*color, 1.0)
        ctx.arc(16, 16, 8, 0, 2 * math.pi)
        ctx.fill()
        # specular highlight
        ctx.set_source_rgba(1, 1, 1, 0.6)
        ctx.arc(13, 13, 3, 0, 2 * math.pi)
        ctx.fill()

        surf.write_to_png(out_path)

        # Update the indicator's icon
        import os
        if os.path.exists(out_path):
            try:
                # Pass full path as the icon name
                self.indicator.set_icon_full(out_path, aggregate_state)
            except Exception:
                pass

    def refresh_from_sessions(self) -> None:
        """Compute aggregate state and update icon."""
        sess = snapshot()
        if not sess:
            self._refresh_icon("idle")
            return
        states = {s.state for s in sess}
        if "failure" in states:
            aggregate = "failure"
        elif "needs_perm" in states:
            aggregate = "needs_perm"
        elif "busy" in states:
            aggregate = "busy"
        elif "success" in states:
            aggregate = "success"
        else:
            aggregate = "idle"
        self._refresh_icon(aggregate)




def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bg", action="store_true")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args()
    if args.stop:
        stop_daemon()
        return
    if args.bg:
        daemonize()

    win = LightWindow()

    display = Gdk.Display.get_default()
    mon = display.get_primary_monitor() or display.get_monitor(0)
    geom = mon.get_geometry()
    win.move(geom.x + geom.width - 220, geom.y + 40)

    for key, label, agent, pid in discover_sessions():
        add_or_update(key, label, agent, pid)
    refresh_window_size(win)
    win.show_all()

    def tick():
        win.panel.queue_draw()
        return True
    GLib.timeout_add(33, tick)

    def rediscover():
        discovered = discover_sessions()
        alive = {k for k, _l, _a, _p in discovered}
        for key, label, agent, pid in discovered:
            add_or_update(key, label, agent, pid)
        drop_if_missing(alive)
        autofade_success()
        refresh_window_size(win)
        _log(f"discover: {[(s.key, s.agent, s.model or '-', s.state) for s in snapshot()]}")
        if tray is not None:
            tray.refresh_from_sessions()
        return True
    GLib.timeout_add_seconds(3, rediscover)

    # Initialize the system tray icon (GNOME top bar / notification area)
    try:
        tray = HermesTrayIcon(win)
        _log("tray icon initialized")
    except Exception as e:
        tray = None
        _log(f"tray init failed: {e}")

    socket_server(_log)
    _log(f"hermes-light started, pid={os.getpid()}")
    signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())
    Gtk.main()


if __name__ == "__main__":
    main()
