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

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

SOCKET_PATH = os.path.expanduser("~/.hermes/run/hermes-light.sock")
PIDFILE = os.path.expanduser("~/.hermes/run/hermes-light.pid")
LOGFILE = os.path.expanduser("~/.hermes/logs/hermes-light.log")

# ──── visual constants ───────────────────────────────────────────
COL_W = 64
COL_H = 148
COL_GAP = 8
BADGE_R = 15                 # agent badge circle radius
BADGE_Y = 20                 # badge center y
LIGHT_R = 11                 # status light radius
LIGHT_GAP = 5
SPACING = LIGHT_R * 2 + LIGHT_GAP
LIGHTS_TOP = BADGE_Y + BADGE_R + 12        # y of first light center
PANEL_RADIUS = 14            # rounded-rect corner radius
CHASE_PERIOD_MS = 420        # one full chaser cycle (OpenCode-style marquee)
BOUNCE_AMPL = 6

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


def _load_logo_surface(agent: str) -> cairo.Surface | None:
    """Render the agent's SVG logo to a cairo ImageSurface (cached)."""
    if agent in _rsvg_cache:
        return _rsvg_cache[agent]
    path = os.path.join(LOGO_DIR, AGENTS[agent]["svg"])
    if not os.path.exists(path):
        return None
    try:
        gi.require_version("Rsvg", "2.0")
        from gi.repository import Rsvg  # noqa: F811
        handle = Rsvg.Handle.new_from_file(path)
        dim = handle.get_dimensions()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, dim.width, dim.height)
        ctx = cairo.Context(surf)
        handle.render_cairo(ctx)
        _rsvg_cache[agent] = surf
        return surf
    except Exception:
        return None


# ──── session registry ───────────────────────────────────────────
class Session:
    __slots__ = ("key", "label", "agent", "state")

    def __init__(self, key: str, label: str, agent: str = "hermes"):
        self.key = key
        self.label = label
        self.agent = agent if agent in AGENTS else "hermes"
        self.state = "idle"


sessions_lock = threading.Lock()
sessions: dict[str, Session] = {}
sessions_order: list[str] = []


def add_or_update(key: str, label: str, agent: str = "hermes") -> None:
    with sessions_lock:
        s = sessions.get(key)
        if s is None:
            sessions[key] = Session(key, label, agent)
            sessions_order.append(key)
        else:
            s.label = label
            if agent in AGENTS:
                s.agent = agent


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
        return True


def drop_if_missing(alive: set[str]) -> None:
    with sessions_lock:
        for k in list(sessions_order):
            if k not in alive:
                sessions.pop(k, None)
                sessions_order.remove(k)


def snapshot() -> list[Session]:
    with sessions_lock:
        return [sessions[k] for k in sessions_order]


# ──── agent discovery ────────────────────────────────────────────
def detect_agent(pid: int, comm: str) -> str:
    """Map a process to an agent type.  Checks comm first, then argv0."""
    c = comm.lower()
    if c == "hermes":
        return "hermes"
    if c in ("claude", "claude-code", "claude-code-cli"):
        return "claude"
    if c in ("opencode", "opencode-cli"):
        return "opencode"
    # node / bun shims: peek at argv0
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().decode("utf-8", "replace").split("\0")
    except OSError:
        return ""
    argv0 = args[0].split("/")[-1].lower() if args else ""
    joined = " ".join(args).lower()
    if "claude" in argv0 or "/claude" in joined:
        return "claude"
    if "opencode" in argv0 or "opencode" in joined:
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
        agent = detect_agent(int(pid_s), comm)
        if not agent:
            continue
        # Resolve the tty (ps tty column) for a stable label
        try:
            tty_out = subprocess.check_output(
                ["ps", "-p", pid_s, "-o", "tty="], text=True
            ).strip()
        except subprocess.CalledProcessError:
            tty_out = "?"
        if tty_out == "?":
            key = f"{agent}:{pid_s}"
            label = f"{agent} {pid_s}"
        else:
            key = f"{tty_out}:{pid_s}"
            label = tty_out
        if key in seen:
            continue
        seen.add(key)
        found.append((key, label, agent))
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
    """Agent identity badge: brand-color glow + official SVG logo."""
    rgb = AGENTS[agent]["color"]
    # outer glow
    ctx.set_source_rgba(*rgb, 0.35)
    ctx.arc(cx, cy, BADGE_R + 5, 0, 2 * math.pi)
    ctx.fill()
    # disc with radial gradient
    grad = cairo.RadialGradient(cx - BADGE_R * 0.3, cy - BADGE_R * 0.3, BADGE_R * 0.15,
                                cx, cy, BADGE_R)
    grad.add_color_stop_rgba(0, min(1, rgb[0] + 0.3), min(1, rgb[1] + 0.3),
                             min(1, rgb[2] + 0.3), 1.0)
    grad.add_color_stop_rgba(1, rgb[0], rgb[1], rgb[2], 1.0)
    ctx.set_source(grad)
    ctx.arc(cx, cy, BADGE_R, 0, 2 * math.pi)
    ctx.fill()

    # official logo on top, scaled into the disc
    surf = _load_logo_surface(agent)
    if surf is not None:
        sw, sh = surf.get_width(), surf.get_height()
        if sw > 0 and sh > 0:
            scale = (BADGE_R * 1.5) / max(sw, sh)
            ctx.save()
            ctx.translate(cx, cy)
            ctx.scale(scale, scale)
            ctx.translate(-sw / 2, -sh / 2)
            ctx.set_source_surface(surf, 0, 0)
            ctx.paint()
            ctx.restore()
    else:
        # fallback: letter
        label = agent[0].upper()
        ctx.set_source_rgba(1, 1, 1, 0.95)
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(BADGE_R * 1.15)
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

        # translucent rounded panel
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)
        _rounded_rect(ctx, 0, 0, w, h, PANEL_RADIUS)
        ctx.set_source_rgba(0.05, 0.05, 0.08, 0.72)
        ctx.fill()
        ctx.set_line_width(1.0)
        ctx.set_source_rgba(1, 1, 1, 0.08)
        _rounded_rect(ctx, 0.5, 0.5, w - 1, h - 1, PANEL_RADIUS)
        ctx.stroke()

        now = time.time()
        chase_phase = (now * 1000 / CHASE_PERIOD_MS) % 1.0
        blink = 0.45 + 0.55 * (0.5 + 0.5 * math.cos(now * 2 * math.pi))

        for i, s in enumerate(sess):
            cx = COL_W / 2 + i * (COL_W + COL_GAP)

            # badge
            _badge(ctx, cx, BADGE_Y, s.agent)

            # lights
            if s.state == "busy":
                # OpenCode-style chaser: lights 0→1→2→1→0, blue.
                # Triangle wave over 3 positions, period = CHASE_PERIOD_MS.
                tri = 2.0 - abs(2.0 - (chase_phase * 4.0) % 4.0)   # 0..2..0
                cur = min(2, max(0, int(round(tri))))
                layout = [("dim", 0), ("dim", 1), ("dim", 2)]
                layout[cur] = ("blue", cur)
            elif s.state == "needs_perm":
                layout = [("dim", 0), ("yellow", 1), ("dim", 2)]
            elif s.state == "success":
                layout = [("dim", 0), ("dim", 1), ("green", 2)]
            elif s.state == "failure":
                layout = [("red", 0), ("dim", 1), ("dim", 2)]
            else:
                layout = [("dim", 0), ("dim", 1), ("dim", 2)]

            for color, idx in layout:
                cy = LIGHTS_TOP + idx * SPACING
                rgb = STATUS_COLORS[color]
                alpha = 1.0
                if color == "dim":
                    alpha = 0.55
                elif s.state == "failure" and color == "red":
                    alpha = blink
                _light(ctx, cx, cy, rgb, alpha, LIGHT_R)
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
            Gtk.grab_add(self)
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
            Gtk.grab_remove(self)
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

    for key, label, agent in discover_sessions():
        add_or_update(key, label, agent)
    refresh_window_size(win)
    win.show_all()

    def tick():
        win.panel.queue_draw()
        return True
    GLib.timeout_add(33, tick)

    def rediscover():
        discovered = discover_sessions()
        alive = {k for k, _l, _a in discovered}
        for key, label, agent in discovered:
            add_or_update(key, label, agent)
        drop_if_missing(alive)
        refresh_window_size(win)
        _log(f"discover: {[(s.key, s.agent, s.state) for s in snapshot()]}")
        return True
    GLib.timeout_add_seconds(3, rediscover)

    socket_server(_log)
    _log(f"hermes-light started, pid={os.getpid()}")
    signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())
    Gtk.main()


if __name__ == "__main__":
    main()
