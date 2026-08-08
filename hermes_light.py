"""
hermes-light — always-on-top GTK window showing one RGBY light column per
running Hermes CLI session.  Each column reflects that session's state in
real time.

Layout:
  [col_0] [col_1] [col_2] ...   horizontally, left-to-right
  Each column is 76x130, with three stacked circles:
      top    red   (failure / blink)
      middle yellow (needs permission)
      bottom green (success)

Communication: the GUI listens on a Unix datagram socket at
~/.hermes/run/hermes-light.sock.  Two message shapes:

  Hook → GUI (state update):
    {"session": "pts/0", "pid": 54990, "state": "busy"}
    {"session": "pts/0", "pid": 54990, "state": "needs_perm"}
    {"session": "pts/0", "pid": 54990, "state": "success"}
    {"session": "pts/0", "pid": 54990, "state": "failure"}
    {"session": "pts/0", "pid": 54990, "state": "idle"}

  GUI → Hook (ack after success):
    {"ack": true, "session": "pts/0"}

When no hook is pushing state, columns are auto-discovered from
running `hermes` processes (mirroring hermes-watch) — state defaults
to idle and lights up only when a hook message arrives.

Clicking or pressing 'a' on a green column sends the ack and turns it
idle.  Dragging the window anywhere moves it.

Run:
  hermes-light                # foreground, Ctrl-C to stop
  hermes-light --bg           # fork into background
  hermes-light --stop         # stop the bg instance
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

# ──── visual ──────────────────────────────────────────────────────
COL_W = 56
COL_H = 130
COL_GAP = 6
CIRCLE_R = 11
SPACING = CIRCLE_R * 2 + 4
BOUNCE_AMPL = 6
BOUNCE_PERIOD_MS = 700

COLORS = {
    "blue":   (0.30, 0.62, 1.00),
    "yellow": (1.00, 0.83, 0.20),
    "green":  (0.36, 0.84, 0.46),
    "red":    (1.00, 0.36, 0.36),
    "dim":    (0.18, 0.18, 0.20),
}

STATE_TO_COLOR = {
    "busy":       "blue",
    "needs_perm": "yellow",
    "success":    "green",
    "failure":    "red",
    "idle":       "dim",
}


# ──── session state ──────────────────────────────────────────────
class Session:
    __slots__ = ("key", "label", "state", "ack_pending")

    def __init__(self, key: str, label: str):
        self.key = key            # stable id (e.g. "pts/0:54990" or just "pts/0")
        self.label = label        # short text under the column
        self.state = "idle"
        self.ack_pending = False  # True when success is showing, awaiting user ack


sessions_lock = threading.Lock()
sessions: dict[str, Session] = {}
sessions_order: list[str] = []


def add_or_update_session(key: str, label: str | None = None) -> Session:
    with sessions_lock:
        s = sessions.get(key)
        if s is None:
            s = Session(key, label or key)
            sessions[key] = s
            sessions_order.append(key)
        elif label:
            s.label = label
        return s


def set_session_state(key: str, state: str) -> bool:
    if state not in STATE_TO_COLOR:
        return False
    with sessions_lock:
        s = sessions.get(key)
        if s is None:
            s = Session(key, key)
            sessions[key] = s
            sessions_order.append(key)
        s.state = state
        s.ack_pending = (state == "success")
        return True


# ──── drawing area ───────────────────────────────────────────────
class LightGrid(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_size_request(COL_W, COL_H)
        self.connect("draw", self._on_draw)

    def _on_draw(self, widget, ctx: cairo.Context):
        with sessions_lock:
            order = list(sessions_order)
            snap = {k: sessions[k].state for k in order}

        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)

        now = time.time()
        bounce = -math.cos((now * 1000 / BOUNCE_PERIOD_MS) * 2 * math.pi) * BOUNCE_AMPL
        blink = 0.4 + 0.6 * (0.5 + 0.5 * math.cos(now * 2 * math.pi))

        for i, key in enumerate(order):
            st = snap[key]
            cx = COL_W / 2
            first_cy = (COL_H - (3 * SPACING - 4)) / 2

            if st == "busy":
                positions = [("busy", "blue"), ("dim", "dim"), ("dim", "dim")]
            elif st == "failure":
                positions = [("failure", "red"), ("dim", "dim"), ("dim", "dim")]
            elif st == "needs_perm":
                positions = [("dim", "dim"), ("needs_perm", "yellow"), ("dim", "dim")]
            elif st == "success":
                positions = [("dim", "dim"), ("dim", "dim"), ("success", "green")]
            else:
                positions = [("dim", "dim"), ("dim", "dim"), ("dim", "dim")]

            offset_x = i * (COL_W + COL_GAP)
            for j, (_state_name, color_name) in enumerate(positions):
                cy = first_cy + j * SPACING
                if j == 0 and st == "busy":
                    cy += bounce
                rgb = COLORS.get(color_name, COLORS["dim"])
                alpha = 1.0
                if color_name == "dim":
                    alpha = 0.5
                elif st == "failure" and color_name == "red":
                    alpha = blink
                ctx.set_source_rgba(*rgb, alpha * 0.30)
                ctx.arc(offset_x + cx, cy, CIRCLE_R + 3, 0, 2 * math.pi); ctx.fill()
                ctx.set_source_rgba(*rgb, alpha)
                ctx.arc(offset_x + cx, cy, CIRCLE_R, 0, 2 * math.pi); ctx.fill()
                ctx.set_source_rgba(1, 1, 1, alpha * 0.45)
                ctx.arc(offset_x + cx - CIRCLE_R * 0.3, cy - CIRCLE_R * 0.3,
                        CIRCLE_R * 0.35, 0, 2 * math.pi); ctx.fill()
        return False


# ──── discovery ───────────────────────────────────────────────────
def discover_hermes_sessions() -> list[tuple[str, str]]:
    """Return [(key, label)] for every running `hermes` CLI process.

    Auto-adds them as idle sessions so the GUI shows one column per session
    even before the hook pushes any state.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,comm=,tty="], text=True
        )
    except subprocess.CalledProcessError:
        return []
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or parts[1] != "hermes":
            continue
        pid, _comm, tty = parts
        # ps already prefixes the tty column with "pts/" (e.g. "pts/0").  If
        # it's "?" the process has no controlling terminal.
        if tty == "?":
            key = f"pid:{pid}"
            label = f"pid {pid}"
        else:
            key = f"{tty}:{pid}"            # "pts/0:54990"
            label = tty                     # "pts/0"
        if key in seen:
            continue
        seen.add(key)
        found.append((key, label))
    return found


# ──── window ──────────────────────────────────────────────────────
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

        self.grid = LightGrid()
        self.add(self.grid)
        self.grid.show()

        # Drag state — set by button-press, consumed by motion-notify.
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
        # Left button → start drag (and possibly ack on click-without-drag)
        if event.button == 1:
            self._drag_start_xy = (event.x_root, event.y_root)
            x, y = self.get_position()
            self._drag_start_win_xy = (x, y)
            self._drag_active = False
            # Grab pointer so motion events are routed to us even if the
            # cursor leaves the window mid-drag — otherwise the WM steals
            # the gesture and we never see the motion events.
            Gtk.grab_add(self)
            return True
        if event.button == 3:
            # Right-click: ack whichever column was clicked
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
            self._ack_first_success()
            return True
        return False

    def _column_at(self, x: float) -> str | None:
        with sessions_lock:
            order = list(sessions_order)
        if not order:
            return None
        idx = int(x // (COL_W + COL_GAP))
        if 0 <= idx < len(order):
            return order[idx]
        return None

    def _ack_at(self, x: float):
        key = self._column_at(x)
        if key is None:
            return
        with sessions_lock:
            s = sessions.get(key)
            if s is None or s.state != "success":
                return
            s.state = "idle"
            s.ack_pending = False
        _send_ack(key)

    def _ack_first_success(self):
        with sessions_lock:
            for k in sessions_order:
                if sessions[k].state == "success":
                    sessions[k].state = "idle"
                    sessions[k].ack_pending = False
                    _send_ack(k)
                    return


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
    if n == 0:
        n = 1
    w = n * COL_W + (n - 1) * COL_GAP
    # set_default_size + queue_resize are needed because plain .resize() is
    # ignored on a decorated=False / set_resizable(False) window.
    win.set_default_size(w, COL_H)
    win.resize(w, COL_H)
    win.queue_resize()
    # Tell the DrawingArea the new size too — its on_draw uses (COL_W, COL_H)
    # per column, but its widget-level size_request is still (COL_W, COL_H).
    if hasattr(win, "grid"):
        win.grid.set_size_request(w, COL_H)


# ──── socket server ──────────────────────────────────────────────
def socket_server(write_log):
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    if os.path.exists(SOCKET_PATH):
        try: os.unlink(SOCKET_PATH)
        except OSError: pass
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
            label = msg.get("pty") or key
            if msg.get("ack"):
                write_log(f"ack from {key}")
                continue
            state = msg.get("state") or msg.get("kind")
            if state in STATE_TO_COLOR:
                GLib.idle_add(set_session_state, key, state)
                GLib.idle_add(add_or_update_session, key, label)
                write_log(f"recv: {key} → {state}")

    threading.Thread(target=_serve, daemon=True).start()
    return srv


# ──── background mode ────────────────────────────────────────────
def daemonize():
    if os.path.exists(PIDFILE):
        try:
            old = int(open(PIDFILE).read().strip())
            os.kill(old, 0)
            print(f"already running pid={old}", file=sys.stderr); sys.exit(1)
        except (OSError, ValueError):
            pass
    os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
    rfd = os.open(LOGFILE, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(rfd, 1); os.dup2(rfd, 2)
    if os.fork() > 0: sys.exit(0)
    os.setsid()
    if os.fork() > 0: sys.exit(0)
    open(PIDFILE, "w").write(str(os.getpid()))


def stop_daemon():
    if not os.path.exists(PIDFILE):
        print("not running"); return
    pid = int(open(PIDFILE).read().strip())
    try: os.kill(pid, signal.SIGTERM); print(f"sent SIGTERM to {pid}")
    except OSError as e: print(f"failed: {e}")


# ──── log ────────────────────────────────────────────────────────
def _log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
        with open(LOGFILE, "a") as f: f.write(line + "\n")
    except OSError: pass


# ──── main ───────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bg", action="store_true")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args()
    if args.stop: stop_daemon(); return
    if args.bg: daemonize()

    win = LightWindow()

    display = Gdk.Display.get_default()
    mon = display.get_primary_monitor() or display.get_monitor(0)
    geom = mon.get_geometry()
    win.move(geom.x + geom.width - 200, geom.y + 48)

    # Seed with currently-running hermes sessions.
    discovered = discover_hermes_sessions()
    _log(f"discover initial: {discovered}")
    for key, label in discovered:
        add_or_update_session(key, label)
    refresh_window_size(win)
    _log(f"sessions after seed: {list(sessions_order)}")
    win.show_all()

    # Animation tick — redraws so bouncing + blinking stay smooth.
    def tick():
        win.grid.queue_draw()
        return True
    GLib.timeout_add(33, tick)

    # Periodically rediscover hermes sessions (new ones appear, old ones exit).
    def rediscover():
        discovered = discover_hermes_sessions()
        for key, label in discovered:
            add_or_update_session(key, label)
        alive = {k for k, _ in discovered}
        with sessions_lock:
            for k in list(sessions_order):
                if k not in alive:
                    sessions.pop(k, None)
                    sessions_order.remove(k)
        refresh_window_size(win)
        _log(f"rediscover: {list(sessions_order)}")
        return True
    GLib.timeout_add_seconds(3, rediscover)

    socket_server(_log)
    _log(f"hermes-light started, pid={os.getpid()}")
    signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())
    Gtk.main()


if __name__ == "__main__":
    main()
