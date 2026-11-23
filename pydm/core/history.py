"""Download history (SQLite): statistics and duplicate detection."""
import os
import sqlite3
import threading
import time
import urllib.parse

from .util import APP_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    host TEXT,
    filename TEXT,
    path TEXT,
    size INTEGER,
    sha256 TEXT,
    category TEXT,
    seconds REAL,
    avg_speed REAL,
    finished REAL,
    level TEXT
);
CREATE INDEX IF NOT EXISTS ix_url ON downloads(url);
CREATE INDEX IF NOT EXISTS ix_sha ON downloads(sha256);
CREATE INDEX IF NOT EXISTS ix_finished ON downloads(finished);
"""


def _norm(url):
    p = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((p.scheme.lower(), (p.netloc or "").lower(), p.path, p.query,
                                    ""))


class History:
    def __init__(self, path=None):
        self.path = path or os.path.join(APP_DIR, "history.db")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()
        if os.name == "posix":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def record(self, d, seconds):
        size = d.total or 0
        with self.lock:
            self.db.execute(
                "INSERT INTO downloads (url, host, filename, path, size, sha256, category, "
                "seconds, avg_speed, finished, level) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (_norm(d.url), urllib.parse.urlsplit(d.url).hostname, d.filename, d.path, size,
                 d.sha256, d.category, seconds, size / seconds if seconds > 0 else 0,
                 d.finished or time.time(), (d.security or {}).get("level")))
            self.db.commit()

    def _rows(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def find_url(self, url):
        """Most recent completed download of this URL whose file still exists."""
        for row in self._rows("SELECT filename, path, finished, size FROM downloads WHERE url=? "
                              "ORDER BY finished DESC LIMIT 5", (_norm(url),)):
            if row[1] and os.path.exists(row[1]):
                return {"filename": row[0], "path": row[1], "finished": row[2], "size": row[3]}
        return None

    def find_hash(self, sha256, exclude_path=None):
        for row in self._rows("SELECT filename, path, finished FROM downloads WHERE sha256=? "
                              "ORDER BY finished DESC LIMIT 10", (sha256,)):
            if row[1] and row[1] != exclude_path and os.path.exists(row[1]):
                return {"filename": row[0], "path": row[1], "finished": row[2]}
        return None

    def stats(self, days=14):
        now = time.time()
        day = 86400
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

        def agg(since):
            r = self._rows("SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(SUM(seconds),0) "
                           "FROM downloads WHERE finished>=?", (since,))[0]
            return {"count": r[0], "bytes": r[1], "avg_speed": r[1] / r[2] if r[2] else 0}
        daily = []
        for i in range(days - 1, -1, -1):
            start = midnight - i * day
            r = self._rows("SELECT COALESCE(SUM(size),0) FROM downloads WHERE finished>=? AND "
                           "finished<?", (start, start + day))[0]
            daily.append((time.strftime("%a %d", time.localtime(start)), r[0]))
        hosts = self._rows("SELECT host, COUNT(*), SUM(size), SUM(size)/MAX(SUM(seconds),0.001) "
                           "FROM downloads GROUP BY host ORDER BY SUM(size) DESC LIMIT 8")
        cats = self._rows("SELECT category, COUNT(*), SUM(size) FROM downloads GROUP BY category "
                          "ORDER BY SUM(size) DESC")
        best = self._rows("SELECT MAX(avg_speed) FROM downloads")[0][0] or 0
        threats = self._rows("SELECT COUNT(*) FROM downloads WHERE level='danger'")[0][0]
        return {"today": agg(midnight), "week": agg(midnight - 6 * day), "all": agg(0),
                "daily": daily, "hosts": hosts, "categories": cats, "best_speed": best,
                "threats": threats}

    def search(self, text="", limit=200):
        like = f"%{text}%"
        return self._rows("SELECT filename, url, path, size, finished, avg_speed FROM downloads "
                          "WHERE filename LIKE ? OR url LIKE ? ORDER BY finished DESC LIMIT ?",
                          (like, like, limit))

    def clear(self):
        with self.lock:
            self.db.execute("DELETE FROM downloads")
            self.db.commit()
