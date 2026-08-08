#!/usr/bin/env python3
"""
hermes-watch — daemon that watches running `hermes` CLI processes and plays
audio cues when each one transitions between idle and busy.

Detection strategy (no hook framework, no gateway, no code changes to Hermes):
  - Sample the process group CPU% (hermes PID + all its MCP/subagent children)
  - "idle":  group CPU% < IDLE_THRESHOLD for IDLE_SECS consecutive samples
  - "busy":  group CPU% >= BUSY_THRESHOLD for BUSY_SECS consecutive samples

Audio:
  - idle (becomes idle after being busy)  →  success.wav (the "loss" descending tone)
  - busy (becomes busy after being idle)  →  start.wav (滴滴滴滴)
  - failure: not detected here (no semantic access to agent output).
            Use the gateway hook for that.

Run:
  hermes-watch               # foreground, Ctrl-C to stop
  hermes-watch --daemon       # background, log to ~/.hermes/logs/hermes-watch.log
  hermes-watch --stop         # stop the daemon

Auto-discovery: scans `ps` for any process whose comm is `hermes` AND whose
parent is a shell (i.e. an interactive CLI session), excluding the daemon itself.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

SOUNDS_DIR = Path("/home/kali/.local/share/hermes-task-sound")
PIDFILE = Path.home() / ".hermes" / "run" / "hermes-watch.pid"
LOGFILE = Path.home() / ".hermes" / "logs" / "hermes-watch.log"

# Tunables (overridable via CLI flags)
IDLE_THRESHOLD = 5.0     # group CPU% below this counts as idle
BUSY_THRESHOLD = 20.0    # group CPU% above this counts as busy
IDLE_SECS = 2.5          # must stay idle this long → fire "success" cue
BUSY_SECS = 1.0          # must stay busy this long → fire "start" cue
POLL_INTERVAL = 0.25     # sampling cadence
SAMPLE_WINDOW = 1.0      # ps sample window in seconds (cumulative)
VOLUME = 0.6
PLAYER_CANDIDATES = ("paplay", "aplay", "mpv", "ffplay")


@dataclass
class ProcState:
    pid: int
    pty: str           # /dev/pts/N — for human-readable logging
    last_state: str = "unknown"          # "idle" | "busy" | "unknown"
    since: float = field(default_factory=time.time)
    children: dict[int, float] = field(default_factory=dict)  # pid → last cpu sample
    parent_cpu: float = 0.0
    children_cpu: float = 0.0
    last_alert_at: float = 0.0           # for rate-limiting audio per process


# ──────────────────────────── audio ────────────────────────────

def _pick_player() -> str | None:
    for c in PLAYER_CANDIDATES:
        if subprocess.run(["which", c], capture_output=True).returncode == 0:
            return c
    return None


def _play(sound_name: str) -> None:
    path = SOUNDS_DIR / sound_name
    if not path.is_file():
        _log(f"missing sound: {path}")
        return
    player = _pick_player()
    if player is None:
        _log("no audio player found (install paplay / aplay / mpv / ffplay)")
        return
    try:
        if player == "paplay":
            cmd = ["paplay", f"--volume={int(VOLUME * 65535)}", str(path)]
        elif player == "aplay":
            cmd = ["aplay", "-q", str(path)]
        elif player == "mpv":
            cmd = ["mpv", "--no-terminal", "--really-quiet",
                   f"--volume={int(VOLUME * 100)}", str(path)]
        else:                               # ffplay
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                   "-volume", str(int(VOLUME * 100)), str(path)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:                  # noqa: BLE001
        _log(f"playback failed: {e}")


def _alert(kind: str, pty: str) -> None:
    _log(f"→ {kind:7s}  {pty}")
    # Map CPU-watcher's "start"/"success" onto the GUI's state vocabulary.
    gui_state = {"start": "busy", "success": "success"}.get(kind)
    if gui_state is not None:
        _push_gui_state(gui_state, pty)
    if kind == "start":
        _play("start.wav")
    elif kind == "success":
        _play("success.wav")


# ──────────────────────── GUI integration ──────────────────────

GUI_SOCKET = Path.home() / ".hermes" / "run" / "hermes-light.sock"


def _push_gui_state(state: str, info: str) -> None:
    """Best-effort push of a state update to the hermes-light GUI.

    `info` looks like "pts/0 (pid 54990, 100.3% CPU)" — extract the pid.
    The GUI keys its columns by `<pts>:<pid>` so we must match that format.
    """
    if not GUI_SOCKET.exists():
        return
    # Pull the (pid NNNN) group out of the info string.
    m = re.search(r"pid\s+(\d+)", info)
    if not m:
        return
    pid = m.group(1)
    pts_m = re.match(r"(pts/\S+)", info)
    pty = pts_m.group(1) if pts_m else "?"
    session_key = f"{pty}:{pid}"
    payload = json.dumps({
        "session": session_key,
        "pty": pty,
        "state": state,
        "ts": time.time(),
    }).encode()
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.sendto(payload, GUI_SOCKET)
        s.close()
    except OSError:
        pass                                              # GUI not running — silently skip


# ──────────────────────── logging ──────────────────────────────

def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with LOGFILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ──────────────────────── discovery ────────────────────────────

def discover_hermes_pids(self_pid: int) -> dict[int, ProcState]:
    """Find running `hermes` CLI processes (excluding the daemon)."""
    out = subprocess.check_output(
        ["ps", "-eo", "pid=,comm=,tty="], text=True
    )
    pids: dict[int, ProcState] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, comm, tty = parts
        if comm != "hermes":
            continue
        try:
            pid_i = int(pid)
        except ValueError:
            continue
        if pid_i == self_pid or pid_i == os.getpid():
            continue
        pty = tty if tty != "?" else "(no-tty)"
        pids[pid_i] = ProcState(pid=pid_i, pty=pty)
    return pids


# ──────────────────────── sampling ─────────────────────────────

def sample_cpu(pid: int) -> float:
    """Return instantaneous CPU% by diffing two ps samples ~POLL_INTERVAL apart.

    ps's `cputime` is cumulative CPU time. Divide by the elapsed wall-clock
    between two samples to get the *recent* CPU% — which drops to 0 the moment
    the process goes idle.
    """
    snap = _ps_cpu_snapshot(pid)
    now = time.time()
    prev = _LAST_CPU.get(pid)
    _LAST_CPU[pid] = (snap, now)
    if prev is None:
        return 0.0
    prev_snap, prev_time = prev
    dt = now - prev_time
    if dt <= 0:
        return 0.0
    dc = max(0.0, snap - prev_snap)
    return (dc / dt) * 100.0


_LAST_CPU: dict[int, tuple[float, float]] = {}


def _ps_cpu_snapshot(pid: int) -> float:
    """Cumulative CPU seconds used by `pid` (read straight from /proc — no ps cache).

    /proc/<pid>/stat exposes kernel clock ticks since process start in fields
    14 (utime) and 15 (stime). Reading /proc directly bypasses ps's caching and
    gives fresh values every call — critical when our sample interval is
    sub-second.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            content = f.read().decode("ascii", errors="replace")
    except (OSError, FileNotFoundError):
        return 0.0
    # The comm field is the second token and is wrapped in parens; it may
    # contain spaces or even a ')' so split on the LAST ')' to find the
    # boundary between comm and the rest of the fields.
    rp = content.rfind(")")
    if rp < 0:
        return 0.0
    after = content[rp + 1:].split()
    # Field index in `after` (comm is removed):
    #   0=state, 1=ppid, ..., 11=utime, 12=stime
    try:
        utime = int(after[11])
        stime = int(after[12])
    except (IndexError, ValueError):
        return 0.0
    ticks = utime + stime
    # USER_HZ is normally 100 on Linux.  Read it dynamically when possible.
    hz = _CLK_TCK or 100
    return ticks / hz


_CLK_TCK: int = 0


def _init_clk_tck() -> None:
    global _CLK_TCK
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:        # arbitrary file we can stat
            pass
        import os
        # The standard source: getconf CLK_TCK — but in-process we just use 100
        # and override via getconf if it's available.
        r = subprocess.run(["getconf", "CLK_TCK"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip().isdigit():
            _CLK_TCK = int(r.stdout.strip())
            return
    except Exception:
        pass
    _CLK_TCK = 100


def _hms_to_seconds(s: str) -> float:
    if "-" in s:
        days, rest = s.split("-", 1)
        return int(days) * 86400 + _hms_to_seconds(rest)
    bits = s.split(":")
    total = 0.0
    for b in bits:
        total = total * 60 + float(b)
    return total


def children_of(pid: int) -> list[int]:
    """Return alive child PIDs (one level deep — hermes spawns watchdog wrappers
    that spawn the real workers, so going one level catches everything)."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", str(pid)], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return []
    return [int(x) for x in out.split() if x.isdigit() and int(x) > 0]


def group_cpu(state: ProcState) -> float:
    """Sum CPU% of the parent + its children."""
    now = time.time()
    p_cpu = sample_cpu(state.pid)
    children = children_of(state.pid)
    c_cpu = sum(sample_cpu(c) for c in children)
    state.children = {c: sample_cpu(c) for c in children}
    state.parent_cpu = p_cpu
    state.children_cpu = c_cpu
    return p_cpu + c_cpu


# ──────────────────────────── main loop ────────────────────────

def run(stop_event: threading.Event) -> None:
    LOGFILE.parent.mkdir(parents=True, exist_ok=True)
    _init_clk_tck()
    _log(f"hermes-watch started, pid={os.getpid()} (CLK_TCK={_CLK_TCK})")
    self_pid = os.getpid()
    tracked: dict[int, ProcState] = {}

    while not stop_event.is_set():
        try:
            current = discover_hermes_pids(self_pid)
            # remove stale
            for pid in list(tracked):
                if pid not in current:
                    _log(f"  ✗ pid {pid} ({tracked[pid].pty}) exited")
                    del tracked[pid]
            # add new
            for pid, st in current.items():
                if pid not in tracked:
                    tracked[pid] = st
                    st.last_state = "unknown"   # don't fire on first sight
                    st.since = time.time()
                    _log(f"  + watching pid {pid} ({st.pty})")
            # evaluate each
            now = time.time()
            debug = os.getenv("HERMES_WATCH_DEBUG")
            for pid, st in tracked.items():
                cpu = group_cpu(st)
                observed = "busy" if cpu >= BUSY_THRESHOLD else (
                          "idle" if cpu <= IDLE_THRESHOLD else "mid")
                if debug:
                    _log(f"  ? pid {pid} cpu={cpu:6.2f}% observed={observed} state={st.last_state}")
                # transitions
                if st.last_state == "unknown":
                    if observed in ("idle", "busy"):
                        st.last_state = observed
                        st.since = now
                    continue
                if observed == st.last_state:
                    continue
                # mid = transitional, don't react yet
                if observed == "mid":
                    continue
                dur = now - st.since
                if st.last_state == "busy" and observed == "idle" and dur >= IDLE_SECS:
                    # rate-limit: don't alert more than once per 3s per process
                    if now - st.last_alert_at >= 3.0:
                        _alert("success", f"{st.pty} (pid {pid}, {cpu:.1f}% CPU)")
                        st.last_alert_at = now
                    st.last_state = "idle"
                    st.since = now
                elif st.last_state == "idle" and observed == "busy" and dur >= BUSY_SECS:
                    if now - st.last_alert_at >= 3.0:
                        _alert("start", f"{st.pty} (pid {pid}, {cpu:.1f}% CPU)")
                        st.last_alert_at = now
                    st.last_state = "busy"
                    st.since = now
                else:
                    # not enough dwell time — just update anchor without firing
                    st.since = now
            stop_event.wait(POLL_INTERVAL)
        except Exception as e:                # noqa: BLE001
            _log(f"loop error: {e}")
            stop_event.wait(1.0)
    _log("hermes-watch stopped")


def daemonize() -> None:
    """DEPRECATED — use the systemd user service instead. Left here so existing
    invocations of `hermes-watch --daemon` print a hint rather than failing."""
    print("`hermes-watch --daemon` is deprecated.", file=sys.stderr)
    print("Install the systemd user service instead:", file=sys.stderr)
    print("  hermes-watch install     # writes ~/.config/systemd/user/hermes-watch.service", file=sys.stderr)
    print("  systemctl --user enable --now hermes-watch", file=sys.stderr)
    sys.exit(2)


def stop_daemon() -> None:
    if not PIDFILE.is_file():
        print("not running")
        return
    pid = int(PIDFILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"sent SIGTERM to {pid}")
    except OSError as e:
        print(f"failed: {e}")


SERVICE_UNIT = """\
[Unit]
Description=Hermes process watcher — plays audio cues when local Hermes CLI sessions go idle/busy
Documentation=https://github.com/jeremygan2021/hermes-hook-task-done-sound
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def install_service() -> None:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "hermes-watch.service"
    exec_start = (
        f"{sys.executable} -u -c "
        f"'import sys; sys.path.insert(0, \"{Path(__file__).resolve().parent}/../lib/hermes-watch\"); "
        f"import hermes_watch; hermes_watch.run(__import__(\"threading\").Event())'"
    )
    unit_path.write_text(SERVICE_UNIT.format(exec_start=exec_start))
    print(f"wrote {unit_path}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "hermes-watch"], check=False)
    print("enabled + started. Check status:")
    print("  systemctl --user status hermes-watch")
    print("  journalctl --user -u hermes-watch -f")


def uninstall_service() -> None:
    subprocess.run(["systemctl", "--user", "disable", "--now", "hermes-watch"], check=False)
    unit_path = Path.home() / ".config" / "systemd" / "user" / "hermes-watch.service"
    if unit_path.is_file():
        unit_path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        print(f"removed {unit_path}")
    print("uninstalled.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--daemon", action="store_true", help="deprecated — use install")
    ap.add_argument("--stop", action="store_true", help="stop the running service")
    ap.add_argument("--install", action="store_true",
                    help="install systemd user service (writes .service file + enables it)")
    ap.add_argument("--uninstall", action="store_true",
                    help="disable and remove systemd user service")
    args = ap.parse_args()

    if args.stop:
        stop_daemon()
        return
    if args.install:
        install_service()
        return
    if args.uninstall:
        uninstall_service()
        return
    if args.daemon:
        daemonize()

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        run(stop)
    finally:
        if PIDFILE.is_file():
            try:
                PIDFILE.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()
