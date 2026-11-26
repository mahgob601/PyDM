"""Internet quota guardian.

Egyptian (and many other) home plans come with a monthly quota (WE, Vodafone, Orange…).
PyDM counts usage for the current billing cycle and protects what's left:

  * mode "pydm": only PyDM's downloads count; mode "pc": everything this PC sends and
    receives (that's what the ISP counts, minus other devices at home)
  * the cycle restarts on the plan's renewal day; a manual correction lets you match the
    ISP's own figure ("I've already used 40 GB")
  * warning at 80 %; at the stop level (default 95 %) PyDM pauses its downloads and holds the
    queue until the user says "continue anyway" or the cycle renews
  * optional free hours: traffic inside the window isn't counted, and big downloads can be
    held for it
"""
import calendar
import datetime as dt
import json
import os
import threading
import time

from .log import log

try:
    import psutil
except ImportError:                     # whole-PC mode just isn't offered without it
    psutil = None

GB = 1000 ** 3                          # ISPs sell decimal gigabytes


def _now():
    return dt.datetime.now()


def cycle_start(renew_day, now=None):
    """Most recent renewal date at or before now (renew_day 31 → last day of short months)."""
    now = now or _now()
    day = min(renew_day, calendar.monthrange(now.year, now.month)[1])
    start = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if start > now:
        y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        start = start.replace(year=y, month=m,
                              day=min(renew_day, calendar.monthrange(y, m)[1]))
    return start


def next_renewal(renew_day, now=None):
    s = cycle_start(renew_day, now)
    y, m = (s.year, s.month + 1) if s.month < 12 else (s.year + 1, 1)
    return s.replace(year=y, month=m, day=min(renew_day, calendar.monthrange(y, m)[1]))


def in_window(start, end, now=None):
    """'02:00'–'08:00' style window; may wrap past midnight. Empty = never."""
    if not start or not end or start == end:
        return False
    t = (now or _now()).strftime("%H:%M")
    return start <= t < end if start < end else (t >= start or t < end)


def system_counter():
    if psutil is None:
        return None
    c = psutil.net_io_counters()
    return c.bytes_recv + c.bytes_sent


class Quota:
    def __init__(self, manager, path):
        self.m = manager
        self.path = path
        self.lock = threading.Lock()
        self.state = self._load()
        self._last_pydm = manager.bytes_in
        self._last_sys = system_counter()
        self.events = []                 # (level, title, text) for the UI to show

    # ---------------------------------------------------------------- settings
    @property
    def s(self):
        return self.m.settings

    def enabled(self):
        return bool(self.s.get("quota_enabled")) and float(self.s.get("quota_gb") or 0) > 0

    def limit(self):
        return float(self.s.get("quota_gb") or 0) * GB

    # ---------------------------------------------------------------- state
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict):
                return st
        except (OSError, ValueError):
            pass
        return {}

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _roll(self, now):
        start = cycle_start(int(self.s.get("quota_renew_day") or 1), now).isoformat()
        if self.state.get("cycle") != start:
            if self.state.get("cycle"):
                log.info("quota: new cycle from %s (last cycle used %.2f GB)", start,
                         self.used() / GB)
            self.state = {"cycle": start, "pydm": 0, "pc": 0, "adjust": 0,
                          "warned": False, "held": False, "override": False}
            self._save()

    # ---------------------------------------------------------------- counting
    def tick(self, now=None):
        """Call every second or so. Returns True while downloads must be held."""
        now = now or _now()
        with self.lock:
            cur_p, cur_s = self.m.bytes_in, system_counter()
            dp = max(0, cur_p - self._last_pydm)
            ds = 0
            if cur_s is not None and self._last_sys is not None:
                ds = cur_s - self._last_sys
                if ds < 0:                        # counters restarted (sleep/adapter reset)
                    ds = 0
            self._last_pydm, self._last_sys = cur_p, cur_s
            if not self.enabled():
                return False
            self._roll(now)
            if not in_window(self.s.get("quota_free_start"), self.s.get("quota_free_end"), now):
                self.state["pydm"] += dp
                self.state["pc"] += ds
            return self._guard()

    def used(self):
        mode = self.s.get("quota_mode", "pydm")
        base = self.state.get("pc" if mode == "pc" and psutil else "pydm", 0)
        return max(0, base + self.state.get("adjust", 0))

    def set_used(self, gb):
        """Match the ISP's own number: 'I've used 40 GB so far'."""
        with self.lock:
            self._roll(_now())
            mode = self.s.get("quota_mode", "pydm")
            base = self.state.get("pc" if mode == "pc" and psutil else "pydm", 0)
            self.state["adjust"] = int(float(gb) * GB) - base
            self.state["warned"] = self.state["held"] = False
            self._save()

    def allow_anyway(self):
        with self.lock:
            self.state["override"] = True
            self.state["held"] = False
            self._save()
        log.info("quota: user chose to continue past the stop level")

    # ---------------------------------------------------------------- protection
    def _guard(self):
        lim = self.limit()
        frac = self.used() / lim if lim else 0
        stop = float(self.s.get("quota_stop_percent") or 95) / 100
        if frac >= 0.8 and not self.state.get("warned"):
            self.state["warned"] = True
            self.events.append(("caution", "80% of your internet quota is used",
                                self.summary()))
            log.warning("quota: 80%% used (%s)", self.summary())
        if frac >= stop and not self.state.get("override"):
            if not self.state.get("held"):
                self.state["held"] = True
                self.events.append(("danger", "Downloads paused to protect your quota",
                                    f"{self.summary()}. Continue anyway from PyDM, or they "
                                    "resume when your quota renews."))
                log.warning("quota: stop level reached, holding downloads (%s)", self.summary())
            self._save()
            return True
        if self.state.get("held") and frac < stop:
            self.state["held"] = False
        if int(time.time()) % 30 == 0:
            self._save()
        return False

    def held(self):
        return self.enabled() and bool(self.state.get("held")) and \
            not self.state.get("override")

    def big_waits(self, d, now=None):
        """Hold a big download until the free-hours window (if the user wants that)."""
        s = self.s
        if not (self.enabled() and s.get("quota_big_wait") and s.get("quota_free_start")):
            return False
        if in_window(s.get("quota_free_start"), s.get("quota_free_end"), now):
            return False
        left = (d.total or d.expected_total or 0) - d.downloaded
        return left >= float(s.get("quota_big_mb") or 500) * 2**20

    # ---------------------------------------------------------------- display
    def numbers(self, now=None):
        now = now or _now()
        rd = int(self.s.get("quota_renew_day") or 1)
        lim, used = self.limit(), self.used()
        start, end = cycle_start(rd, now), next_renewal(rd, now)
        days_left = max(0.0, (end - now).total_seconds() / 86400)
        elapsed = max(0.25, (now - start).total_seconds() / 86400)
        rate = used / elapsed                         # bytes per day so far
        runout = None
        if rate > 0 and used < lim:
            eta = now + dt.timedelta(days=(lim - used) / rate)
            if eta < end:
                runout = eta
        return {"used": used, "limit": lim, "left": max(0, lim - used),
                "fraction": used / lim if lim else 0, "days_left": days_left,
                "per_day": max(0, lim - used) / max(days_left, 1), "renews": end,
                "runout": runout, "held": self.held()}

    def summary(self, now=None):
        n = self.numbers(now)
        return (f"{n['used'] / GB:.1f} of {n['limit'] / GB:.0f} GB used, "
                f"{n['days_left']:.0f} days left")
