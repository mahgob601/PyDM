"""Keep the computer awake while downloads are running."""
import os
import shutil
import subprocess
import sys


class SleepInhibitor:
    def __init__(self):
        self.active = False
        self.proc = None

    def set(self, on):
        on = bool(on)
        if on == self.active:
            return
        self.active = on
        try:
            if sys.platform.startswith("win"):
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
            elif on:
                if sys.platform == "darwin" and shutil.which("caffeinate"):
                    cmd = ["caffeinate", "-i", "-w", str(os.getpid())]
                elif shutil.which("systemd-inhibit"):
                    cmd = ["systemd-inhibit", "--what=sleep:idle", "--who=PyDM",
                           "--why=Downloading files", "--mode=block", "sleep", "infinity"]
                else:
                    return
                self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
            elif self.proc:
                self.proc.terminate()
                self.proc = None
        except Exception:  # noqa: BLE001 — never let power management break downloads
            self.proc = None
