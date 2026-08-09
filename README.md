# hermes-task-sound

Audio + GUI notifications when your local `hermes` CLI sessions go busy /
idle / awaiting-approval / done. Works for `hermes`, `claude`, `opencode`,
and any other agent whose process `comm` is one of those three.

Two install paths, pick the one that matches your setup:

| Path | For | Install |
|------|-----|---------|
| **`hermes-watch`** (recommended) | CLI users running one or more `hermes` / `claude` / `opencode` sessions in terminals | `bash install-service.sh` |
| Gateway hook (from upstream) | Gateway / Telegram / Discord / Feishu users | `bash install.sh` |

The watcher daemon is the path for **local terminal sessions** — it doesn't
need the gateway running and doesn't require any code changes to Hermes.

## What `hermes-watch` does

It watches every agent CLI process and signals on four events:

- **busy** (LLM streaming / tool exec / subagent running) → `start.wav` (滴滴)
- **needs_perm** (tool call awaiting your approval in the terminal) →
  `needs_perm.wav` ("等待授权")
- **done** (final answer, whole task finished) → `success.wav` (当当
  descending 880→660→440 Hz) + GUI light turns **green**
- **failure** → silent (you see the failed response in the terminal)

State is per-process, keyed by `<tty>:<pid>`. Rate-limited to 1 alert per
3s per process so a chatty session doesn't spam you. Also pushes a small
always-on-top GTK widget (`hermes-light`) showing one column per running
session with its badge, model name, uptime, and current state.

## Detection (priority order)

1. **Hermes lifecycle hooks** (authoritative) — when Hermes runs with
   `hooks:` configured in `~/.hermes/config.yaml`, the
   `hermes-hook-status` script writes `/tmp/hermes-hook-<pid>.state`
   on every `pre_llm_call`, `pre_tool_call`, `pre_approval_request`, and
   `post_api_request`. The watcher reads this file and acts on the
   AUTHORITATIVE state — busy / needs_perm / done.

2. **Sentinel files** — the `hermes` wrapper writes
   `/tmp/hermes-status-<pid>` on launch and `/tmp/hermes-done-<pid>`
   on exit (rename to `.acked` to prevent re-fire). This catches the
   case where hooks aren't configured but the wrapper is in use.

3. **TCP-queue + CPU sampling** (fallback) — when neither hook nor
   wrapper is active (e.g. a Hermes launched by another launcher that
   skipped both). Reads the process's socket inodes from `/proc/<pid>/fd`
   and matches them against `/proc/net/tcp`. A socket with queued data
   (rx_queue > 0) means an LLM response is currently streaming. CPU%
   from `/proc/<pid>/stat` is the secondary signal for tool exec /
   compile / tests. Both sample sub-second via two reads of `utime+stime`
   ticks — `ps -o cputime=` is cached at 1s resolution and gives 0% for
   the wrong half of every busy spike.

## Recent behavior fixes

- **`needs_perm` is no longer sticky.** When the hook transitions out of
  `needs_perm` (approval answered, hook file now says busy again), the
  watcher now proactively pushes `busy` to the GUI socket — previously
  the GUI was stuck showing the approval prompt until the next
  unrelated event, because its 12-second "recent push" window swallowed
  the follow-up hook state.
- **Hook-less sessions get a `done` announcement.** opencode and any
  Hermes launched without the wrapper used to fall into a silent
  busy→idle path: no audio, no green light. Now the watcher waits
  `QUIET_FALLBACK_SECS` of total quiet (TCP empty + CPU below
  threshold) on a hook-less session before committing to "done". Short
  inter-tool gaps stay silent so multi-step runs don't false-fire.
- **Session numbers release on close.** Previously a new session
  always got `max(existing)+1`, so the column numbers crept up
  forever. Now `assign_number` picks the smallest positive integer not
  currently held in `~/.hermes/hermes-light-numbers.json`, and a new
  `reap_dead_numbers()` call at the start of every loop iteration
  drops entries whose pid is gone. The same session keeps its number
  across watcher restarts; a new session reclaims the lowest gap.

## Files

| File | Purpose |
|------|---------|
| `hermes_watch.py` | the daemon (Python stdlib only) |
| `hermes_light.py` | the GTK status widget |
| `hermes-hook-status` | shell script Hermes calls from `hooks:` (writes the `.state` file) |
| `install-service.sh` | writes `~/.config/systemd/user/hermes-watch.service` and `enable --now`s it |
| `start.wav` | 滴滴 (4 × 70 ms beeps at 880 Hz) |
| `success.wav` | 当当 descending (880 → 660 → 440 Hz, fading volume) |
| `error.wav` | reserved / unused (failure path is silent) |
| `generate_tones.py` | regenerates the WAVs from Python stdlib `wave` + `struct` |

## Install

```bash
mkdir -p ~/.local/bin ~/.local/share/hermes-task-sound
cp hermes_watch.py ~/.local/bin/hermes-watch
cp start.wav success.wav error.wav ~/.local/share/hermes-task-sound/

# as a systemd user service (survives logout, auto-restarts on crash)
cp install-service.sh ~/.local/bin/hermes-watch-install
bash ~/.local/bin/hermes-watch-install
```

Or run it directly in a terminal:

```bash
hermes-watch                  # foreground, Ctrl-C to stop
```

## Usage

After install:

```bash
systemctl --user status hermes-watch
journalctl --user -u hermes-watch -f
hermes-watch --uninstall      # remove service
```

The daemon auto-discovers `hermes` / `claude` / `opencode` processes by
their `comm`, so you don't need to "register" anything when you open a
new session. New sessions show up in the widget within ~1s of launch;
exited sessions disappear within ~3s.

## Tunables

Edit at the top of `hermes_watch.py` (`systemctl --user restart hermes-watch`
after):

| Constant | Default | Meaning |
|----------|---------|---------|
| `BUSY_THRESHOLD` | 40.0 % | CPU% above this = busy (fallback only — TCP-queue is the primary signal) |
| `IDLE_THRESHOLD` | 2.0 % | CPU% below this = idle (legacy) |
| `QUIET_SECS` | 10.0 s | hook-driven session: this long quiet after a hook event → assume done |
| `QUIET_FALLBACK_SECS` | 8.0 s | hook-less session: this long quiet → assume done |
| `FALLBACK_BUSY_HOLD` | 3.0 s | after last activity, keep busy this long (absorbs stream-drain gaps) |
| `IDLE_CONFIRM_SECS` | 20.0 s | busy→idle must persist this long before done (fallback) |
| `HOOK_STALE_SECS` | 60.0 s | hook `.state` file older than this is ignored |
| `POLL_INTERVAL` | 0.25 s | sample cadence |
| `VOLUME` | 0.6 | paplay volume (0–1) |

## How it works

Per tracked PID:

1. Discover via `ps -eo pid=,comm=,tty=` filtering `comm ∈ {"hermes",
   "claude", "opencode"}`.
2. Read `/tmp/hermes-hook-<pid>.state` (authoritative; written by Hermes
   itself). If fresh → act on it.
3. If no hook file: read `/tmp/hermes-status-<pid>` (wrapper sentinel).
   If it exists and the session has prior hook history, hold busy for
   `QUIET_SECS` of quiet → done.
4. Else: TCP-queue + CPU sampling. A socket in `/proc/<pid>/fd` with
   non-zero `rx_queue` (from `/proc/net/tcp`) = actively streaming.
   CPU via two reads of `/proc/<pid>/stat` field 14+15 (utime+stime
   ticks). TCP is primary; CPU is the secondary signal for tool exec.
5. State machine: `unknown` → first observed → transitions fire alerts
   + GUI push via `/home/kali/.hermes/run/hermes-light.sock` (DGRAM).

`/proc/<pid>/stat` is the trick that makes CPU sampling sub-second
accurate — `ps -o cputime=` is cached to 1-second resolution and gives
you `0%` for the wrong half of every busy spike.

## Verified

With two real Hermes CLI sessions running (`pts/0` and `pts/1`):

```
[15:23:31] + watching pid 181970 (pts/0) #6
[15:23:31] + watching pid 190842 (pts/1) #1
[15:23:31] ▲ pid 181970 (pts/0) hook says BUSY
[15:23:31] → start    pts/0 (pid=181970)
[15:23:31] ▲ pid 190842 (pts/1) first sight BUSY
[15:23:31] → start    pts/1 (pid=190842)
[15:23:34] · pid 181970 (pts/0) busy → idle (quiet 3.1s, silent)
[15:23:34] · pid 190842 (pts/1) busy → idle (quiet 3.1s, silent)
[15:23:35] ▲ pid 181970 (pts/0) hook says BUSY
[15:23:35] → start    pts/0 (pid=181970)
```

`#6` reuses its number across watcher restarts; `#1` was reclaimed by a
new session after the previous `#1`'s pid exited (the prior session's
entry was reaped from `hermes-light-numbers.json`).

Ad-hoc verification of the fallback quiet behavior:

```
PASS  Bug 1: _push_gui_state('busy') called on resolved
PASS  Bug 1: last_state = busy
PASS  Bug 2 short gap (2s < 8s): no done alert
PASS  Bug 2 short gap: GUI pushed idle
PASS  Bug 2 long gap (10s ≥ 8s): _alert('done')
PASS  Bug 2 long gap: last_state = success
PASS  Bug 2 zero baseline: no done (guard works)
PASS  QUIET_FALLBACK_SECS defined & positive: value=8.0
```

---

## Gateway hook (upstream-derived)

The `HOOK.yaml` + `handler.py` pair implements SunneeYang's hook pattern
for users on the Hermes **gateway** (Telegram/Discord/Feishu/etc.). It's
included for completeness — install with:

```bash
bash install.sh
hermes gateway restart
```

See SunneeYang/hermes-hook-task-done for the original.

## License

Inherits upstream's license.