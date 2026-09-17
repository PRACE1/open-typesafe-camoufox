"""Best-effort cleanup of orphaned cap/camoufox/chromium processes left by a
crashed prior run, so a new recording doesn't fight over the screen, mic,
and lock files."""

from __future__ import annotations

import os
import subprocess

from logging_utils import log

def kill_stale_processes() -> None:
    """Kill orphaned cap/camoufox/chromium processes from a crashed prior run.

    A previous run that timed out or was killed can leave cap.exe (recording),
    camoufox.exe (browser host), and chromium.exe (Camoufox's browser engine)
    holding the screen, mic, and lock files. Starting a new recording while
    those are alive produces blank capture or "screen already in use" errors.
    This is a best-effort sweep; a missing process is not fatal.
    """
    targets = ["cap.exe", "camoufox.exe", "chromium.exe", "firefox.exe", "geckodriver.exe"]
    killed: list[str] = []
    for name in targets:
        try:
            result = subprocess.run(
                f"taskkill /F /IM {name} /T",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                killed.append(name)
        except Exception:
            pass
    if killed:
        log(f"[preflight] killed stale processes: {', '.join(killed)}")
    else:
        log("[preflight] no stale cap/camoufox/chromium processes found")

    # Also kill any lingering Python agent processes (prior crashed runs).
    try:
        result = subprocess.run(
            'tasklist /FI "IMAGENAME eq python.exe" /FO CSV /NH',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        pids: list[str] = []
        for line in result.stdout.strip().splitlines():
            parts = line.strip('"').split(',')
            if len(parts) >= 2:
                pid_str = parts[1].strip()
                try:
                    pid = int(pid_str)
                    if pid != os.getpid():
                        pids.append(str(pid))
                except ValueError:
                    pass
        if pids:
            for pid in pids:
                subprocess.run(f"taskkill /F /PID {pid}", shell=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log(f"[preflight] killed {len(pids)} stale Python agent process(es)")
    except Exception:
        pass
