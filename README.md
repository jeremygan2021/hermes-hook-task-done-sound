# hermes-task-sound

Audio notifications when your local Hermes CLI sessions go idle / busy.

Two ways to use it:

| Path | For | Install |
|------|-----|---------|
| **`hermes-watch`** (recommended) | CLI users running one or more `hermes` sessions in terminals | `cp hermes-watch ~/.local/bin/ && bash install-service.sh` |
| Gateway hook (from upstream) | Gateway / Telegram / Discord / Feishu users | `bash install.sh` (from SunneeYang's pattern) |

The watcher daemon is the path for **local terminal sessions** — it doesn't
need the gateway running and doesn't require any code changes to Hermes.

## `hermes-watch` — process-group CPU watcher

Detects when a `hermes` process (plus its MCP-server / subagent children)
transitions between **idle** (CPU% < 5) and **busy** (CPU% > 20):

- **idle → busy** sustained for ≥1s → plays **start.wav** ("滴滴")
- **busy → idle** sustained for ≥2.5s → plays **success.wav** ("当当" — a descending 880→660→440 Hz loss-feel tone)
- **failure** → silent (no signal in the process-group layer; you already see the failed response on the terminal)

State is per-process. Rate-limited to 1 alert per 3s per process so a chatty
session doesn't spam you.

### Files

| File | Purpose |
|------|---------|
| `hermes-watch` | the daemon (Python, stdlib only) |
| `install-service.sh` | writes `~/.config/systemd/user/hermes-watch.service` and `enable --now`s it |
| `start.wav` | 滴滴 (4 × 70 ms beeps at 880 Hz) |
| `success.wav` | 当当 descending (880 → 660 → 440 Hz, fading volume) |
| `error.wav` | shipped but unused (failure path is silent by default) |
| `generate_tones.py` | regenerates the WAVs from Python stdlib `wave` + `struct` |

### Install

```bash
mkdir -p ~/.local/bin
cp hermes-watch ~/.local/bin/
mkdir -p ~/.local/share/hermes-task-sound
cp start.wav success.wav error.wav ~/.local/share/hermes-task-sound/

# as a systemd user service (survives logout, auto-restarts on crash)
cp install-service.sh ~/.local/bin/hermes-watch-install
bash ~/.local/bin/hermes-watch-install
```

Or run it directly in a terminal:

```bash
hermes-watch                  # foreground, Ctrl-C to stop
```

### Usage

After install:

```bash
systemctl --user status hermes-watch
journalctl --user -u hermes-watch -f
hermes-watch --uninstall      # remove service
```

Tunables (edit at the top of `hermes-watch`):

| Constant | Default | Meaning |
|----------|---------|---------|
| `IDLE_THRESHOLD` | 5.0 % | below this = idle |
| `BUSY_THRESHOLD` | 20.0 % | above this = busy |
| `IDLE_SECS` | 2.5 s | must stay idle this long before "success" fires |
| `BUSY_SECS` | 1.0 s | must stay busy this long before "start" fires |
| `POLL_INTERVAL` | 0.25 s | sample cadence |
| `VOLUME` | 0.6 | paplay volume (0–1) |

### How it works

Per tracked Hermes PID:
1. Discover via `ps -eo pid=,comm=,tty=` filtering `comm == "hermes"`.
2. Sample CPU% via **two reads of `/proc/<pid>/stat` field 14+15** (utime+stime ticks) — bypasses `ps`'s caching and gives sub-second-accurate instantaneous CPU%.
3. Walk the process tree one level deep (`pgrep -P PID`) and sum children.
4. State machine: `unknown` → first observed → transitions fire alerts.

`/proc/<pid>/stat` is the trick that makes this work — `ps -o cputime=` is
cached to 1-second resolution on Linux and gives you `0%` for the wrong half
of every busy spike.

### Verified

On 2026-08-08 with three real Hermes CLI sessions running:

```
[16:02:36] → start    (no-tty) (pid 200928, 107.2% CPU)
[16:02:37] → start    pts/0 (pid 54990, 81.2% CPU)
[16:02:40] → success  (no-tty) (pid 200928, 0.0% CPU)
[16:02:41] → success  pts/0 (pid 54990, 2.9% CPU)
```

PID 200928 was a fake Hermes (comm set via `setproctitle`); 54990 is a real
session running an agent task.

---

## Gateway hook (upstream-derived)

The `HOOK.yaml` + `handler.py` pair implements SunneeYang's hook pattern for
users on the Hermes **gateway** (Telegram/Discord/Feishu/etc.). It's
included for completeness — install with:

```bash
bash install.sh
hermes gateway restart
```

See SunneeYang/hermes-hook-task-done for the original.

## License

Inherits upstream's license.
