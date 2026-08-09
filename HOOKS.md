# Official Hermes lifecycle hooks → status bridge

Hermes CLI has a first-class **shell-hooks** mechanism that fires **in CLI mode**
(no gateway needed). Events include:

```
on_session_start, pre_llm_call, post_llm_call, pre_api_request,
post_api_request, pre_tool_call, post_tool_call,
pre_approval_request, post_approval_response,
subagent_start, subagent_stop, on_session_end, ...
```

The hook script reads the event JSON on stdin and writes a tiny status file
keyed by the hermes process PID (the hook subprocess's PPID):

```
/tmp/hermes-hook-<pid>.state      "busy|needs_perm <ts>"
```

hermes-watch / hermes-light read this file as the **authoritative** state:

| Hermes event              | status file | light        |
|---------------------------|-------------|--------------|
| pre/post llm_call, tool_call | busy      | BLUE (running) |
| pre_approval_request      | needs_perm   | RED blinking (授权) |
| (quiet, awaiting input)   | file gone    | GREEN (done)  |
| on_session_end            | file removed | off           |

The green "done" light means **the agent finished its turn and is waiting for
your next input** — not "the last API call returned".

## Install

1. Copy `hermes-hook-status` to a stable path (e.g. `/home/kali/bin/`).

2. Append to `~/.hermes/config.yaml`:

```yaml
hooks_auto_accept: true
hooks:
  pre_llm_call:
    - command: /home/kali/bin/hermes-hook-status
      timeout: 2
  pre_tool_call:
    - command: /home/kali/bin/hermes-hook-status
      timeout: 2
  post_tool_call:
    - command: /home/kali/bin/hermes-hook-status
      timeout: 2
  post_llm_call:
    - command: /home/kali/bin/hermes-hook-status
      timeout: 2
  post_api_request:
    - command: /home/kali/bin/hermes-hook-status
      timeout: 2
  pre_approval_request:
    - command: /home/kali/bin/hermes-hook-status
      timeout: 2
  post_approval_response:
    - command: /home/kali/bin/hermes-hook-status
      timeout: 2
  on_session_end:
    - command: /home/kali/bin/hermes-hook-status
      timeout: 2
```

3. **Restart your hermes CLI session** (hooks load at startup). Verify with:

```bash
grep "shell hook registered" ~/.hermes/logs/agent.log
```

## Fallbacks (in order)

1. **Official hook file** — the agent itself says what it's doing.
2. **Sentinel file** (`/tmp/hermes-status-<pid>`, wrapper-launched) — process
   alive; quiet-settle only for hook-driven sessions.
3. **CPU/IO sampling** — bursty agents (opencode/node) stay BLUE while any
   activity happened within the last `QUIET_SECS`; only total silence flips
   to idle → green.
