"""Locate the managed Chrome's main browser process.

Codex's record-and-replay captures OS input (not CDP), so its demo is driven
with real CGEvent clicks aimed at that process.
"""
from __future__ import annotations

import subprocess


def main_chrome_pid(user_data_dir: str) -> int:
    """The main browser process (no --type=) for the managed profile."""
    out = subprocess.run(["ps", "-Axo", "pid,command"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if user_data_dir in line and "--type=" not in line and "grep" not in line:
            return int(line.split()[0])
    raise RuntimeError("managed Chrome main process not found")
