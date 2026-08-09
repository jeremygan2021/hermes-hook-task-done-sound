#!/usr/bin/env python3
"""
hermes-watch — daemon that watches running `hermes` CLI processes and plays
audio cues when each one transitions between idle and busy.

Detection strategy (priority order):
  1. **Sentinel files** (100% accurate — written by the hermes wrapper):
     - /tmp/hermes-status-<pid>  exists  →  agent is busy
     - /tmp/hermes-done-<pid>    appears →  agent just finished (success beat)
  2. **CPU sampling** (fallback for hermes launches that bypass the wrapper):
     - busy:  group CPU% >= BUSY_THRESHOLD for BUSY_SECS consecutive samples
     - idle:  group CPU% <  IDLE_THRESHOLD for IDLE_SECS consecutive samples
     - CPU is unreliable: hermes CLI spends ~99% of wall clock waiting on the
       LLM stream, so a "busy" agent often shows 0% CPU. Sentinels fix this.

Audio + GUI channel is shared so a single event produces exactly one
audio+light update (no more "light says green but no sound" mismatch).

Audio:
  - busy  →  start.wav  (滴滴滴滴)
  - done  →  success.wav (the "loss" descending tone)
  - failure: not detected here (no semantic access to agent output).

Run:
  hermes-watch               # foreground, Ctrl-C to stop
  hermes-watch --daemon      # deprecated — use the systemd user service
  hermes-watch --install    # install + start the systemd user service
  hermes-watch --stop        # stop the running daemon

Auto-discovery: scans `ps` for any process whose comm is `hermes` AND whose
parent is a shell (i.e. an interactive CLI session), excluding the daemon itself.
"""
from __future__ import annotations

import argparse
import glob
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
STATUS_DIR = Path("/tmp")

# Tunables (overridable via CLI flags)
IDLE_THRESHOLD = 2.0     # group CPU% below this counts as idle (legacy fallback)
BUSY_THRESHOLD = 40.0    # CPU% for real work (tool runs, compiles); TUI
                         # animations (opencode spinner) sit at 5-25% and must
                         # NOT count as busy. TCP connection is the primary
                         # signal; this high CPU bar is a secondary fallback.
IDLE_SECS = 3.0          # must stay idle this long → fire "done" cue (legacy)
BUSY_SECS = 1.0          # must stay busy this long → fire "start" cue (legacy)
FALLBACK_BUSY_HOLD = 3.0  # fallback: keep busy this long after last socket/CPU activity
                          # (absorbs stream-drain gaps between polls)
IDLE_CONFIRM_SECS = 20.0  # fallback: busy→idle must persist this long before done
                          # (absorbs TCP/CPU flapping on hook-less sessions)
QUIET_SECS = 10.0        # FALLBACK ONLY: hook file lost + this long quiet →
                         # assume done. Primary completion signal is the hook
                         # "done" state (final answer via post_api_request,
                         # no tool calls). 10s is a recovery fallback only.
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
    io_prev: int = 0                     # last total rchar bytes (IO activity)
    last_alert_at: float = 0.0           # for rate-limiting audio per process
    last_hook_ts: float = 0.0            # last time an official hook event fired
    last_activity_ts: float = 0.0        # last time CPU/IO showed activity (fallback)
    agent: str = "hermes"                # hermes | claude | opencode
    session_num: int = 0                 # stable number in first-seen order
    tcp_bytes_prev: int | None = None    # last TCP traffic sample (delta detect)


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


def _alert(kind: str, pty: str, pid: int | None = None) -> None:
    """Single channel for both audio + GUI updates. Guarantees they stay in sync.

    `kind` is "start" (busy → fire 滴滴), "done" (success → fire 当当) or
    "needs_perm" (yellow — awaiting user approval).
    `pid` is required so the GUI column can be keyed by pts:N:pid.
    """
    _log(f"→ {kind:7s}  {pty} (pid={pid})")
    # Map watcher events onto the GUI's state vocabulary.
    gui_state = {"start": "busy", "done": "success", "needs_perm": "needs_perm"}.get(kind)
    if gui_state is not None and pid is not None:
        _push_gui_state(gui_state, pty, pid)

    settings = _load_settings()
    sound_on = settings.get("sound_enabled", True)
    tts_on = settings.get("tts_enabled", True)

    if kind == "start":
        if sound_on:
            _play("start.wav")
    elif kind in ("done", "needs_perm"):
        # TTS report takes priority when enabled; else the classic cue
        tts_path = None
        agent = "hermes"
        if tts_on and pid is not None:
            try:
                comm = _comm_of(pid)
            except Exception:
                comm = "hermes"
            agent = detect_agent(pid, comm)
            tts_path = _tts_report_for(agent, kind)
        if tts_path:
            vol = settings.get("tts_volume", 0.7)
            # Announce the session NUMBER first ("一号"), then the report.
            num = session_num(pid) if pid is not None else None
            if num is not None:
                num_wav = os.path.join(
                    str(WAV_DIR_TTS), f"num_{num}.wav")
                if os.path.exists(num_wav):
                    _play_wav_path(num_wav, vol)
                    _sleep_gap()
            _play_wav_path(tts_path, vol)
        elif sound_on:
            _play("success.wav")


def _comm_of(pid: int) -> str:
    """Read a process comm name quickly."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return "hermes"


def _play_wav_path(path: str, volume: float) -> None:
    """Play an arbitrary wav at a given volume (fire-and-forget)."""
    player = _pick_player()
    if player is None:
        return
    try:
        if player == "paplay":
            cmd = ["paplay", f"--volume={int(volume * 65535)}", path]
        elif player == "aplay":
            cmd = ["aplay", "-q", path]
        elif player == "mpv":
            cmd = ["mpv", "--no-terminal", "--really-quiet",
                   f"--volume={int(volume * 100)}", path]
        else:
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                   "-volume", str(int(volume * 100)), path]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:                  # noqa: BLE001
        _log(f"tts playback failed: {e}")


# ──────────────────────── GUI integration ──────────────────────

GUI_SOCKET = Path.home() / ".hermes" / "run" / "hermes-light.sock"


def _push_gui_state(state: str, pty: str, pid: int) -> None:
    """Best-effort push of a state update to the hermes-light GUI.

    The GUI keys its columns by `<pts>:<pid>` so we must match that format.
    """
    if not GUI_SOCKET.exists():
        return
    # Normalize pty: GUI rounds "?" to "(no-tty)" — keep the same convention.
    if pty == "?":
        pts_key = "(no-tty)"
    else:
        pts_key = pty
    session_key = f"{pts_key}:{pid}"
    payload = json.dumps({
        "session": session_key,
        "pty": pts_key,
        "state": state,
        "ts": time.time(),
    }).encode()
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.sendto(payload, str(GUI_SOCKET))
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
        agent = detect_agent(int(pid), comm) if pid.isdigit() else ""
        if not agent:
            continue
        try:
            pid_i = int(pid)
        except ValueError:
            continue
        if pid_i == self_pid or pid_i == os.getpid():
            continue
        pty = tty if tty != "?" else "(no-tty)"
        st = ProcState(pid=pid_i, pty=pty)
        st.agent = agent
        pids[pid_i] = st
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


def _consume_done_tombstones() -> dict[int, str]:
    """Atomically consume /tmp/hermes-done-* files (rename to .acked).

    The wrapper writes these on hermes exit — they are the most reliable
    "agent finished" signal and beat CPU-based detection by 100% accuracy.

    Renaming (instead of unlink) prevents the same beat from being re-fired
    if two daemon iterations race — only the one that wins the rename gets it.
    """
    consumed: dict[int, str] = {}
    pattern = str(STATUS_DIR / "hermes-done-*")
    for path_str in glob.glob(pattern):
        path = Path(path_str)
        if path.suffix == ".acked":
            continue
        try:
            content = path.read_text()
        except OSError:
            continue
        m_pid = re.search(r"pid=(\d+)", content)
        m_tty = re.search(r"tty=(\S+)", content)
        if not m_pid:
            continue
        try:
            pid = int(m_pid.group(1))
        except ValueError:
            continue
        tty = m_tty.group(1) if m_tty else "?"
        try:
            path.rename(path.with_suffix(".acked"))
        except OSError:
            continue
        consumed[pid] = tty
    return consumed


def _sentinel_busy(pid: int) -> bool:
    """True if /tmp/hermes-status-<pid> exists (wrapper wrote it on startup)."""
    return (STATUS_DIR / f"hermes-status-{pid}").exists()


HOOK_STALE_SECS = 60.0   # hook state file older than this is ignored


def _hook_state(pid: int) -> str | None:
    """Read Hermes' official lifecycle hook state for a pid.

    Returns "busy", "needs_perm", "done", or None if no fresh hook state.

    Hermes writes /tmp/hermes-hook-<pid>.state via the shell-hooks mechanism
    (config.yaml hooks: section → hermes-hook-status script). This is the
    AUTHORITATIVE signal — the agent itself says what it's doing:
      - busy       → pre/post llm_call, pre/post tool_call, subagent events,
                     or a post_api_request that requested tool calls
      - needs_perm → pre_approval_request (awaiting user approval)
      - done       → post_api_request with NO tool_calls and finish_reason
                     "stop" — the FINAL answer. The whole task is complete,
                     not just one step. This is the only true "finished".
    """
    try:
        p = STATUS_DIR / f"hermes-hook-{pid}.state"
        if not p.is_file():
            return None
        content = p.read_text().strip()
        parts = content.split()
        if len(parts) < 2:
            return None
        state, ts = parts[0], float(parts[1])
        if time.time() - ts > HOOK_STALE_SECS:
            return None
        return state
    except (OSError, ValueError, IndexError):
        return None


# ──────────────────────── shared settings ──────────────────────
SETTINGS_PATH = Path.home() / ".hermes" / "hermes-light-settings.json"
TTS_WAV_DIR = Path.home() / ".local" / "share" / "hermes-light" / "tts" / "wav"
WAV_DIR_TTS = TTS_WAV_DIR  # alias used by _alert number announcements


def _sleep_gap(seconds: float = 0.7) -> None:
    """Pause between the number announcement and the report so the two
    phrases are clearly separated ("二号" … "OpenCode 已完成")."""
    time.sleep(seconds)

_tts_cache: dict = {}


def _load_settings() -> dict:
    """Cache settings for up to 3s (GUI writes them; we re-read occasionally)."""
    global _tts_cache
    try:
        mtime = SETTINGS_PATH.stat().st_mtime
        if _tts_cache.get("mtime") == mtime:
            return _tts_cache
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        _tts_cache = {"mtime": mtime, **data}
        return _tts_cache
    except OSError:
        return {}


def detect_agent(pid: int, comm: str) -> str:
    """Map a process to an agent type (hermes/claude/opencode).

    Only returns a known agent — everything else gets "" so the watcher
    does NOT track random system processes.
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


# ──────────────────────── session numbering ─────────────────────
# Each agent process gets a stable number in first-seen order, persisted
# across watch restarts so the same session keeps its number. Numbers are
# never reused while a session is alive.
NUMBERS_PATH = Path.home() / ".hermes" / "hermes-light-numbers.json"
_numbers_lock = threading.Lock()


def _load_numbers() -> dict:
    """Load {pid_str: {"num": int, "agent": str, "first_seen": float}}."""
    try:
        with open(NUMBERS_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_numbers(data: dict) -> None:
    try:
        NUMBERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(NUMBERS_PATH) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(NUMBERS_PATH))
    except OSError:
        pass


def assign_number(pid: int, agent: str) -> int:
    """Assign (or recall) the stable session number for a pid, first-seen order.

    Existing sessions keep their number; new sessions get max+1. A pid whose
    session ended and restarted (new pid) gets a fresh number — numbers are
    not recycled while a session is alive.
    """
    with _numbers_lock:
        data = _load_numbers()
        key = str(pid)
        if key in data:
            return int(data[key]["num"])
        # next free number = max existing + 1
        nums = [int(v["num"]) for v in data.values()]
        num = max(nums, default=0) + 1
        data[key] = {"num": num, "agent": agent, "first_seen": time.time()}
        _save_numbers(data)
        return num


def session_num(pid: int) -> int | None:
    """Current number for a pid, or None if not assigned yet."""
    with _numbers_lock:
        data = _load_numbers()
        key = str(pid)
        if key in data:
            return int(data[key]["num"])
        return None


# TTS report wav keyed by agent/event
TTS_REPORT_FILES = {
    "task_done":    "task_done.wav",
    "hermes_done":  "hermes_done.wav",
    "claude_done":  "claude_done.wav",
    "opencode_done": "opencode_done.wav",
    "needs_perm":   "needs_perm.wav",
}


def _tts_report_for(agent: str, event: str) -> str | None:
    """Pick the TTS wav for an agent+event. Returns absolute path or None."""
    if event == "needs_perm":
        key = "needs_perm"
    elif event == "done":
        key = f"{agent}_done" if agent in ("hermes", "claude", "opencode") else "task_done"
    else:
        return None
    fname = TTS_REPORT_FILES.get(key)
    if not fname:
        return None
    p = TTS_WAV_DIR / fname
    return str(p) if p.is_file() else None


def _sample_io_bytes(pid: int) -> int:
    """Cumulative bytes read by pid (rchar from /proc/<pid>/io).

    NOTE: rchar includes terminal reads, so TUI-driven agents (opencode's
    spinner, hermes' prompt) show continuous rchar growth even when idle.
    Use this only as a weak fallback — the TCP-connection check is the
    reliable "calling an LLM API" signal.
    """
    try:
        with open(f"/proc/{pid}/io") as f:
            for line in f:
                if line.startswith("rchar:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _socket_inodes(pid: int) -> set[str]:
    """Inode numbers of all sockets opened by a process."""
    inodes: set[str] = set()
    try:
        fd_dir = f"/proc/{pid}/fd"
        for name in os.listdir(fd_dir):
            try:
                link = os.readlink(f"{fd_dir}/{name}")
            except OSError:
                continue
            if link.startswith("socket:["):
                inodes.add(link[len("socket:["):-1])
    except OSError:
        pass
    return inodes


def _has_live_tcp(pid: int) -> bool:
    """True if the process has an ESTABLISHED TCP connection.

    Reads /proc/net/tcp directly (no sudo needed) and matches the process's
    socket inodes. A connection alone does NOT mean busy — opencode opens a
    keep-alive connection on launch. Busy requires TRAFFIC on it (see
    _group_has_live_tcp, which combines connection + rchar growth).
    """
    inodes = _socket_inodes(pid)
    if not inodes:
        return False
    try:
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            with open(table) as f:
                next(f, None)  # header
                for line in f:
                    parts = line.split()
                    # 0:sl 1:local 2:rem 3:st 4:tx 5:rx ... 9:inode
                    if len(parts) >= 10 and parts[3] == "01" and parts[9] in inodes:
                        return True
    except OSError:
        pass
    return False


def _socket_queue_activity(pid: int) -> int:
    """Total queued bytes (rx_queue + tx_queue) across the process's live
    TCP sockets, sampled from /proc/net/tcp.

    rx_queue > 0 while streaming an LLM response (kernel holds unread data).
    A keep-alive connection that opencode opens on launch has queue 0:0 —
    no data flowing → idle. This cleanly separates "actually streaming"
    from "connection exists but nothing is happening".
    """
    inodes = _socket_inodes(pid)
    if not inodes:
        return 0
    total = 0
    try:
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            with open(table) as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) >= 10 and parts[3] == "01" and parts[9] in inodes:
                        # parts[4] = "tx_queue:rx_queue" (hex)
                        txrx = parts[4].split(":")
                        if len(txrx) == 2:
                            total += int(txrx[0], 16) + int(txrx[1], 16)
    except OSError:
        pass
    return total


def _group_has_live_tcp(state: ProcState) -> bool:
    """True if the process group has TCP sockets WITH queued data.

    A connection alone (opencode's launch keep-alive) is NOT busy. Busy
    means data is actually queued/streaming on a socket — i.e. the agent
    is talking to an LLM API or a tool is doing network I/O.
    """
    total = _socket_queue_activity(state.pid)
    for c in state.children:
        total += _socket_queue_activity(c)
    return total > 0


def group_io_delta(state: ProcState) -> int:
    """Bytes read since last sample across parent + children.

    First sample only seeds the baseline (returns 0) so a freshly-tracked
    process doesn't look like a burst of activity from cumulative rchar.
    """
    total = _sample_io_bytes(state.pid) + sum(_sample_io_bytes(c) for c in state.children)
    if state.io_prev == 0:
        state.io_prev = total
        return 0
    delta = max(0, total - state.io_prev)
    state.io_prev = total
    return delta


# ──────────────────────────── main loop ────────────────────────

def run(stop_event: threading.Event) -> None:
    LOGFILE.parent.mkdir(parents=True, exist_ok=True)
    _init_clk_tck()
    _log(f"hermes-watch started, pid={os.getpid()} (CLK_TCK={_CLK_TCK})")
    self_pid = os.getpid()
    tracked: dict[int, ProcState] = {}

    while not stop_event.is_set():
        try:
            # 1) Tombstone sweep (highest priority — 100% accurate done signal)
            done_tombstones = _consume_done_tombstones()
            for pid, tty in done_tombstones.items():
                pty = tty if tty != "?" else "(no-tty)"
                if pid in tracked:
                    _alert("done", pty, pid)
                else:
                    _log(f"  ! tombstone for unknown pid {pid} ({pty})")
                    _alert("done", pty, pid)
                stale = STATUS_DIR / f"hermes-status-{pid}"
                if stale.exists():
                    stale.unlink()
                tracked.pop(pid, None)

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
                    st.last_activity_ts = 0.0           # no activity history yet
                    # Assign stable session number in first-seen order.
                    st.session_num = assign_number(pid, st.agent)
                    _log(f"  + watching pid {pid} ({st.pty}) #{st.session_num}")
            # evaluate each
            now = time.time()
            debug = os.getenv("HERMES_WATCH_DEBUG")
            for pid, st in tracked.items():
                # 2) OFFICIAL hook state (highest priority — Hermes itself says)
                hstate = _hook_state(pid)
                if hstate == "needs_perm":
                    st.last_hook_ts = now
                    if st.last_state != "needs_perm":
                        _log(f"  ⚠ pid {pid} ({st.pty}) AWAITING APPROVAL")
                        if st.last_state != "busy" or now - st.last_alert_at >= 3.0:
                            _alert("needs_perm", st.pty, pid)
                            st.last_alert_at = now
                        st.last_state = "needs_perm"
                        st.since = now
                    continue
                if hstate == "done":
                    # FINAL answer from Hermes — whole task complete.
                    # Push success immediately (authoritative, no guessing).
                    if st.last_state != "success":
                        _log(f"  ✓ pid {pid} ({st.pty}) hook says DONE (final answer)")
                        _alert("done", st.pty, pid)
                        st.last_alert_at = now
                        st.last_state = "success"
                        st.since = now
                        st.last_hook_ts = now
                    continue
                if st.last_state == "needs_perm":
                    # Approval answered (hook file gone / busy again) — back to work
                    _log(f"  ↻ pid {pid} ({st.pty}) approval resolved → busy")
                    st.last_state = "busy"
                    st.since = now
                if hstate == "busy":
                    st.last_hook_ts = now
                    if st.last_state != "busy":
                        _log(f"  ▲ pid {pid} ({st.pty}) hook says BUSY")
                        if st.last_state in ("idle", "unknown", "success", "needs_perm"):
                            # Any non-busy → busy is a real transition: announce.
                            _alert("start", st.pty, pid)
                            st.last_alert_at = now
                        st.last_state = "busy"
                        st.since = now
                    continue
                # 3) Sentinel busy (wrapper fallback — process alive)
                if _sentinel_busy(pid):
                    if st.last_hook_ts > 0:
                        # Hook-driven hermes: sentinel = alive, hook = activity.
                        # Quiet for QUIET_SECS → awaiting input → DONE (green).
                        if st.last_state == "success":
                            # Already announced done. Do NOT pull back to busy —
                            # that caused an infinite done→busy→done loop with
                            # repeated TTS. Only a NEW hook event (which updates
                            # last_hook_ts in the hstate=="busy" branch above)
                            # restarts the work cycle.
                            continue
                        if st.last_state != "busy":
                            _log(f"  ▲ pid {pid} ({st.pty}) sentinel+hook says BUSY")
                            if st.last_state == "idle":
                                _alert("start", st.pty, pid)
                                st.last_alert_at = now
                            st.last_state = "busy"
                            st.since = now
                        if st.last_state == "busy" and now - st.last_hook_ts >= QUIET_SECS:
                            _log(f"  ✓ pid {pid} ({st.pty}) quiet → DONE (awaiting input)")
                            _alert("done", st.pty, pid)
                            st.last_state = "success"
                            st.since = now
                            st.last_hook_ts = now
                        continue
                    # No hook history (legacy launch / opencode / claude):
                    # sentinel just means the process is alive — fall through to
                    # CPU/IO sampling to decide busy vs idle. Do NOT quiet-settle.
                    pass
                # 4) CPU/IO fallback (no hook file, no sentinel — legacy launch)
                cpu = group_cpu(st)
                io_delta = group_io_delta(st)
                tcp = _group_has_live_tcp(st)
                # PRIMARY: socket with queued data = actually streaming from
                # an API. An opencode that merely opened a keep-alive
                # connection has queue 0:0 → idle. Sampling is instantaneous,
                # so a stream that gets drained between polls still keeps the
                # session busy for FALLBACK_BUSY_HOLD via last_activity_ts.
                # SECONDARY: sustained high CPU (tool exec, compile, tests).
                if tcp or cpu >= BUSY_THRESHOLD:
                    st.last_activity_ts = now
                    observed = "busy"
                elif now - st.last_activity_ts < FALLBACK_BUSY_HOLD:
                    # Recent activity (stream drained between polls) — keep busy.
                    observed = "busy"
                else:
                    observed = "idle"
                if debug:
                    _log(f"  ? pid {pid} cpu={cpu:6.2f}% io={io_delta}B tcp={tcp} state={st.last_state} → {observed} (fallback)")
                # transitions
                if st.last_state == "unknown":
                    # First observation. Announce the observed state so the
                    # GUI never keeps a stale state after a watcher restart.
                    st.last_state = observed
                    st.since = now
                    if observed == "busy":
                        _log(f"  ▲ pid {pid} ({st.pty}) first sight BUSY")
                        _alert("start", st.pty, pid)
                        st.last_alert_at = now
                    elif observed == "idle":
                        _log(f"  · pid {pid} ({st.pty}) first sight idle")
                        _push_gui_state("idle", st.pty, pid)
                    continue
                if observed == st.last_state:
                    continue
                if st.last_state == "busy" and observed == "idle":
                    # NO done announcement for hook-less (fallback) sessions —
                    # we can't reliably distinguish "gap inside a multi-step
                    # turn" from "task finished" without Hermes' own final-
                    # answer signal. But DO push idle to the GUI so the light
                    # turns grey promptly after work stops (no long busy lag).
                    _log(f"  · pid {pid} ({st.pty}) busy → idle (silent)")
                    _push_gui_state("idle", st.pty, pid)
                    st.last_state = "idle"
                    st.since = now
                elif st.last_state in ("idle", "success") and observed == "busy":
                    # New activity (TCP/CPU) — back to work. From success this
                    # is a NEW turn (user sent a message); from idle a resume.
                    if now - st.last_alert_at >= 3.0:
                        _alert("start", st.pty, pid)
                        st.last_alert_at = now
                    st.last_state = "busy"
                    st.since = now
                elif st.last_state == "success" and observed == "idle":
                    # Green already shown; just settle to idle silently.
                    st.last_state = "idle"
                    st.since = now
                # 5) QUIET-SETTLE: hook-driven session went quiet (no busy/perm
                #    events for QUIET_SECS) but the process is alive → the
                #    current turn finished and the agent waits for input = DONE.
                #    This is what turns the light GREEN — not "call finished".
                #    Only for hook-driven sessions (last_hook_ts > 0); pure
                #    CPU/IO processes (opencode etc.) use the idle transition
                #    above and must NOT double-fire.
                if hstate is None and st.last_hook_ts > 0 and st.last_state in ("busy", "needs_perm"):
                    quiet_for = now - max(st.last_hook_ts, st.since)
                    if quiet_for >= QUIET_SECS:
                        _log(f"  ✓ pid {pid} ({st.pty}) quiet {quiet_for:.0f}s → DONE (awaiting input)")
                        _alert("done", st.pty, pid)
                        st.last_state = "success"
                        st.since = now
                        st.last_hook_ts = now
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
