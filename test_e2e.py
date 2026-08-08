"""End-to-end smoke test for task-done-sound.

Simulates the three event paths and verifies paplay is launched with the
correct WAV. Failure path must NOT spawn paplay (default silent config).
"""
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

HERE = Path("/home/kali/dev/hermes-hook-task-done-sound")
spec = importlib.util.spec_from_file_location("tds", HERE / "handler.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def kill_paplay():
    subprocess.run(["pkill", "-f", "paplay"], capture_output=True)


def get_paplay():
    r = subprocess.run(["pgrep", "-af", "paplay"], capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if "pgrep" not in l]


def wait_for_paplay(timeout=2.0):
    """Poll quickly — paplay lives for the duration of a short WAV (~600 ms)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ps = get_paplay()
        if ps: return ps
        time.sleep(0.05)
    return []


def test(event_type, ctx, expect_sound, label):
    kill_paplay()
    time.sleep(0.15)
    m.handle(event_type, ctx)
    if ctx.get("platform") == "cli":
        ps = wait_for_paplay(timeout=1.0)
    else:
        # Poll continuously through the entire delay window — paplay flashes
        # briefly so we can't just sleep and check at the end.
        delay = float(m._load_config().get("delay_seconds", 3))
        deadline = time.time() + delay + 2.0
        ps = []
        while time.time() < deadline and not ps:
            ps = get_paplay()
            if not ps:
                time.sleep(0.05)
        if not ps:
            ps = get_paplay()
    if expect_sound:
        assert ps, f"[FAIL] {label}: expected paplay, got nothing"
        playing = " ".join(ps)
        assert Path(expect_sound).name in playing, \
            f"[FAIL] {label}: wrong wav — got {playing}"
        print(f"  ✓ {label} → {expect_sound.split('/')[-1]}")
    else:
        assert not ps, f"[FAIL] {label}: expected SILENT, got {ps}"
        print(f"  ✓ {label} → silent")


print("Test 1: agent:start (CLI)  → expect 滴滴 (start.wav)")
test("agent:start", {"platform": "cli"}, str(HERE / "start.wav"), "start/cli")

print("Test 2: agent:end success (CLI) → expect 当当 (success.wav)")
test("agent:end", {"platform": "cli", "response": "Patched the bug."},
     str(HERE / "success.wav"), "success/cli")

print("Test 3: agent:end failure (CLI) → expect SILENT (default)")
test("agent:end", {"platform": "cli",
                   "response": "Traceback (most recent call last"}, None,
     "failure/cli")

print("Test 4: agent:end success (feishu) → delayed → expect 当当")
test("agent:end", {"platform": "feishu", "response": "All done."},
     str(HERE / "success.wav"), "success/feishu")

print("Test 5: empty response → success (no failure pattern matched)")
test("agent:end", {"platform": "cli", "response": ""}, str(HERE / "success.wav"),
     "success/empty")

kill_paplay()
print("\nAll 5 tests passed.")
