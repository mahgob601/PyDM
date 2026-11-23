"""Smart bandwidth: time-of-day speed rules, yielding to other apps, quiet mode during
full-screen games / presentations / calls.

The Manager calls Governor.effective_limit() twice a second; the result (bytes/s, 0 = none)
is applied to the global limiter on top of the user's base limit.
"""
import sys
import time

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def rule_active(rule, now=None):
    """rule = {"days": [0..6], "start": "HH:MM", "end": "HH:MM", "kbps": int, "enabled": bool}"""
    if not rule.get("enabled", True):
        return False
    t = time.localtime(now or time.time())
    hm = f"{t.tm_hour:02d}:{t.tm_min:02d}"
    a, b = rule.get("start", "00:00"), rule.get("end", "23:59")
    days = rule.get("days") or list(range(7))
    if a <= b:
        return t.tm_wday in days and a <= hm < b
    # crosses midnight: the part after midnight belongs to the previous day's rule
    if hm >= a:
        return t.tm_wday in days
    return hm < b and (t.tm_wday - 1) % 7 in days


def fullscreen_busy():
    """Windows: True while a full-screen game/app, presentation or 'busy' state is active."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        # 2 = busy (full screen), 3 = Direct3D full screen, 4 = presentation mode,
        # 7 = Windows quiet time / focus
        return state.value in (2, 3, 4)
    except Exception:  # noqa: BLE001
        return False


class Governor:
    def __init__(self, settings, own_speed_fn, net_counter=None, clock=time.monotonic,
                 fullscreen_fn=fullscreen_busy):
        self.settings = settings
        self.own_speed = own_speed_fn          # PyDM's current total bytes/s
        self.clock = clock
        self.fullscreen_fn = fullscreen_fn
        self.net_counter = net_counter if net_counter is not None else self._default_counter()
        self._last = None
        self.other = 0.0                       # other apps' download traffic, bytes/s
        self.capacity = 0.0                    # best total throughput seen (link estimate)
        self._busy_since = None
        self._yield_until = 0.0
        self.reason = ""

    @staticmethod
    def _default_counter():
        try:
            import psutil
            return lambda: psutil.net_io_counters().bytes_recv
        except Exception:  # noqa: BLE001
            return None

    def _sample(self):
        if not self.net_counter:
            return
        now = self.clock()
        total = self.net_counter()
        if self._last is not None:
            dt = now - self._last[0]
            if dt >= 0.4:
                rate = max(0.0, (total - self._last[1]) / dt)
                self.capacity = max(self.capacity * 0.9995, rate)
                own = self.own_speed()
                other = max(0.0, rate - own * 1.04)    # ~4% TCP/IP overhead on our traffic
                self.other = other if self.other == 0 else self.other * 0.6 + other * 0.4
                self._last = (now, total)
        else:
            self._last = (now, total)

    def effective_limit(self):
        """bytes/s limit to apply now (0 = unlimited) and set self.reason."""
        s = self.settings
        limits, reasons = [], []
        base = int(s.get("speed_limit_kbps", 0)) * 1024
        if base:
            limits.append(base)
            reasons.append("speed limit")
        for rule in s.get("speed_rules", []):
            if rule_active(rule):
                limits.append(max(1, int(rule.get("kbps", 0))) * 1024)
                reasons.append(f"rule {rule.get('start')}–{rule.get('end')}")
        if s.get("quiet_when_fullscreen") and self.fullscreen_fn():
            limits.append(max(1, int(s.get("quiet_kbps", 512))) * 1024)
            reasons.append("game / full screen")
        if s.get("yield_to_apps"):
            self._sample()
            now = self.clock()
            threshold = max(150 * 1024, self.capacity * 0.10)
            if self.other > threshold:
                self._busy_since = self._busy_since or now
                if now - self._busy_since >= 2:
                    self._yield_until = now + 10        # keep yielding a bit after it stops
            else:
                self._busy_since = None
            if now < self._yield_until and self.capacity > 0:
                share = int(max(64 * 1024, (self.capacity - self.other) * 0.5,
                                self.capacity * 0.25))
                limits.append(share)
                reasons.append("other apps busy")
        if not limits:
            self.reason = ""
            return 0
        lim = min(limits)
        self.reason = reasons[limits.index(lim)]
        return lim
