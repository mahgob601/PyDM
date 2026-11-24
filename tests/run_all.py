"""Run every PyDM test suite and write REPORT.md + results.json.

  python tests/run_all.py --out results [--suites core,hls] [--work C:\\pydm-ci\\work]
Suites can also be chosen in tests/request.json: {"suites": ["windows", "package"]}.
"""
import argparse
import glob
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
for _s in (sys.stdout, sys.stderr):  # CI consoles on Windows are cp1252; never die on a "→"
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
from tests import fixtures  # noqa: E402
from tests.suites import SUITES  # noqa: E402

SERVERS = {          # name: (root, extra args) — ports assigned at start
    "core": ("srv", ["--per-conn", "3000"]),
    "cdn": ("srv", ["--per-conn", "1000", "--latency", "30"]),
    "flaky": ("srv", ["--per-conn", "1500", "--slow", "0.25", "--latency", "40"]),
    "mirror1": ("srv", ["--global-rate", "6000"]),
    "mirror2": ("srv", ["--global-rate", "6000"]),
    "raw": ("srv", []),
    "hang": ("srv", ["--per-conn", "3000", "--hang", "5"]),        # probe + all 4 go silent
    "hang_some": ("srv", ["--per-conn", "3000", "--hang", "3"]),   # probe + 2 of 4 go silent
    "vid": ("vid", ["--per-conn", "1500", "--latency", "20"]),
    "vid_slow": ("vid", ["--per-conn", "250"]),
}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Ctx:
    def __init__(self, work, out):
        self.W, self.out_dir = work, out
        self.ports, self.procs, self.results, self.metrics, self.notes = {}, [], [], {}, []
        self.suite = ""
        self.ff = self._find("ffmpeg")
        self.ffprobe = self._find("ffprobe")
        self.chrome = self._find_chrome()
        self.free_port = free_port

    @staticmethod
    def _find(name):
        p = shutil.which(name)
        if p:
            return p
        for c in glob.glob(r"C:\ffmpeg\bin\%s.exe" % name) + glob.glob(
                r"C:\pydm-ci\ffmpeg*\bin\%s.exe" % name):
            return c
        return None

    @staticmethod
    def _find_chrome():
        if os.environ.get("CHROME"):
            return os.environ["CHROME"]
        for pat in (r"C:\pydm-ci\chrome\chrome\*\chrome-win64\chrome.exe",
                    os.path.expanduser("~/.cache/puppeteer/chrome/*/chrome-*/chrome*")):
            hits = sorted(glob.glob(pat))
            if hits:
                return hits[-1]
        return shutil.which("google-chrome") or shutil.which("chrome")

    # ---- helpers used by suites
    def p(self, rel):
        return os.path.join(self.W, *rel.split("/"))

    def dir(self, name):
        d = os.path.join(self.W, "runs", name)
        os.makedirs(d, exist_ok=True)
        return d

    def url(self, name):
        return f"http://127.0.0.1:{self.ports[name]}/"

    def start_bench(self, root, port, *args):
        root = root if os.path.isabs(root) else self.p(root)
        pr = subprocess.Popen([sys.executable, os.path.join(HERE, "bench_server.py"), root,
                               str(port), *map(str, args)], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        self.procs.append(pr)
        self._wait_port(port)
        return pr

    def _wait_port(self, port, timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                socket.create_connection(("127.0.0.1", port), 0.5).close()
                return
            except OSError:
                time.sleep(0.2)

    def manager(self, name, **settings):
        from pydm.core.engine import Manager
        m = Manager(persist=False)
        base = {"download_dir": self.dir(name), "use_categories": False,
                "antivirus_scan": False, "mark_of_the_web": False}
        base.update(settings)
        m.update_settings(base)
        return m

    @staticmethod
    def wait(m, d, limit=180):
        t0 = time.time()
        while time.time() - t0 < limit:
            m.schedule()
            if not d.is_running() and (d.status in ("Completed", "Paused") or
                                       (d.status == "Error" and not d.retry_at)):
                break
            time.sleep(0.1)
        return time.time() - t0

    @staticmethod
    def wait_running(m, d, secs):
        t0 = time.time()
        while time.time() - t0 < secs:
            m.schedule()
            time.sleep(0.1)

    def probe(self, path):
        if not path or not os.path.exists(path) or not self.ffprobe:
            return None
        r = subprocess.run([self.ffprobe, "-v", "error", "-show_entries",
                            "format=duration:stream=codec_type,height", "-of", "json", path],
                           capture_output=True, text=True)
        j = json.loads(r.stdout or "{}")
        return (round(float(j.get("format", {}).get("duration", 0))),
                sorted(s["codec_type"] + (str(s["height"]) if s.get("height") else "")
                       for s in j.get("streams", [])))

    def check(self, name, ok, info=""):
        ok = bool(ok)
        self.results.append({"suite": self.suite, "name": name, "ok": ok,
                             "info": str(info)[:500]})
        print(("PASS " if ok else "FAIL ") + f"[{self.suite}] {name}" +
              (f" — {info}" if info else ""), flush=True)

    def skip(self, name, why):
        self.results.append({"suite": self.suite, "name": name, "ok": None, "info": why})
        print(f"SKIP [{self.suite}] {name} — {why}", flush=True)

    def note(self, name, text):
        self.notes.append(f"{self.suite} · {name}: {text}")


def environment(ctx):
    info = {"os": platform.platform(), "python": sys.version.split()[0],
            "cpu_count": os.cpu_count()}
    for mod in ("PySide6", "yt_dlp", "cryptography", "psutil"):
        try:
            m = __import__(mod)
            info[mod] = getattr(m, "__version__", None) or getattr(
                getattr(m, "version", None), "__version__", "installed")
        except ImportError:
            info[mod] = "missing"
    if ctx.ff:
        info["ffmpeg"] = subprocess.run([ctx.ff, "-version"], capture_output=True,
                                        text=True).stdout.split("\n")[0][:80]
    info["chrome"] = ctx.chrome or "missing"
    if sys.platform.startswith("win"):
        ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                             "(Get-MpComputerStatus | Select-Object AMServiceEnabled,"
                             "RealTimeProtectionEnabled,AMProductVersion | ConvertTo-Json)"],
                            capture_output=True, text=True)
        info["defender"] = ps.stdout.strip().replace("\r\n", " ")[:300]
    return info


def write_report(ctx, env, durations, out):
    os.makedirs(out, exist_ok=True)
    total = [r for r in ctx.results if r["ok"] is not None]
    passed = sum(1 for r in total if r["ok"])
    with open(os.path.join(out, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"env": env, "results": ctx.results, "metrics": ctx.metrics,
                   "notes": ctx.notes, "durations": durations}, f, indent=1, ensure_ascii=False)
    lines = [f"# PyDM test report — {passed}/{len(total)} passed", "",
             f"*{time.strftime('%Y-%m-%d %H:%M:%S')}*", "", "## Environment", ""]
    lines += [f"- **{k}**: {v}" for k, v in env.items()]
    lines += ["", "## Results", ""]
    for suite, _fn in SUITES:
        rs = [r for r in ctx.results if r["suite"] == suite]
        if not rs:
            continue
        ok = sum(1 for r in rs if r["ok"])
        lines.append(f"### {suite} — {ok}/{sum(1 for r in rs if r['ok'] is not None)} "
                     f"({durations.get(suite, 0):.0f}s)")
        for r in rs:
            mark = "✅" if r["ok"] else ("⏭️" if r["ok"] is None else "❌")
            lines.append(f"- {mark} {r['name']}" + (f" — `{r['info']}`" if r["info"] else ""))
        lines.append("")
    if ctx.metrics:
        lines += ["## Metrics", ""] + [f"- {k}: {v}" for k, v in ctx.metrics.items()] + [""]
    if ctx.notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in ctx.notes] + [""]
    with open(os.path.join(out, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return passed, len(total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "results"))
    ap.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "pydm-ci-work"))
    ap.add_argument("--suites", default="")
    a = ap.parse_args()
    want = [s for s in a.suites.split(",") if s]
    req = os.path.join(HERE, "request.json")
    if not want and os.path.exists(req):
        want = json.load(open(req)).get("suites", [])
    os.makedirs(a.work, exist_ok=True)
    out = os.path.abspath(a.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    ctx = Ctx(a.work, out)
    env = environment(ctx)
    print(json.dumps(env, indent=1), flush=True)
    durations = {}
    try:
        if not ctx.ff:
            raise SystemExit("ffmpeg not found — install it first")
        fixtures.build(a.work, ctx.ff)
        for name, (root, args) in SERVERS.items():
            ctx.ports[name] = free_port()
            ctx.start_bench(root, ctx.ports[name], *args)
        ctx.ports["site"] = free_port()
        ctx.procs.append(subprocess.Popen([sys.executable, os.path.join(HERE, "servers.py"),
                                           "site", ctx.p("site"), str(ctx.ports["site"])]))
        ctx.ports["ftp"] = free_port()
        ctx.procs.append(subprocess.Popen([sys.executable, os.path.join(HERE, "servers.py"),
                                           "ftp", ctx.p("ftproot"), str(ctx.ports["ftp"]),
                                           "2000000"]))
        for k in ("site", "ftp"):
            ctx._wait_port(ctx.ports[k])
        shutil.rmtree(os.path.join(a.work, "runs"), ignore_errors=True)
        for name, fn in SUITES:
            if want and name not in want:
                continue
            ctx.suite = name
            t0 = time.time()
            print(f"\n=== {name}", flush=True)
            try:
                fn(ctx)
            except Exception as e:  # noqa: BLE001 — one broken suite must not stop the rest
                ctx.check("suite ran without crashing", False,
                          f"{e.__class__.__name__}: {e} | {traceback.format_exc()[-700:]}")
            durations[name] = time.time() - t0
            write_report(ctx, env, durations, out)
    finally:
        for pr in ctx.procs:
            try:
                pr.kill()
            except OSError:
                pass
        passed, total = write_report(ctx, env, durations, out)
        print(f"\nSUMMARY: {passed} / {total} passed", flush=True)


if __name__ == "__main__":
    main()
