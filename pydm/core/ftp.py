"""FTP / FTPS support with multi-connection downloads (REST offsets)."""
import ftplib
import os
import socket
import urllib.parse


class FtpStatusError(IOError):
    def __init__(self, code, msg):
        super().__init__(f"FTP {msg}")
        self.code = code          # mapped to HTTP-like codes so retry logic applies


def _map_error(e):
    text = str(e)
    num = text[:3]
    mapping = {"530": 401, "550": 404, "553": 403, "532": 401, "450": 503, "421": 503,
               "425": 503, "426": 503}
    return FtpStatusError(mapping.get(num, 500 if num.startswith("4") else 403),
                          text.strip()[:200])


def _parts(url):
    p = urllib.parse.urlsplit(url)
    user = urllib.parse.unquote(p.username) if p.username else "anonymous"
    pwd = urllib.parse.unquote(p.password) if p.password else ("anonymous@" if user ==
                                                                 "anonymous" else "")
    return p, user, pwd, urllib.parse.unquote(p.path or "/")


def connect(url, timeout=30, ssl_context=None):
    p, user, pwd, _path = _parts(url)
    try:
        if p.scheme == "ftps":
            ftp = ftplib.FTP_TLS(context=ssl_context, timeout=timeout)
        else:
            ftp = ftplib.FTP(timeout=timeout)
        ftp.connect(p.hostname, p.port or 21)
        ftp.login(user, pwd)
        if p.scheme == "ftps":
            ftp.prot_p()
        ftp.voidcmd("TYPE I")
        return ftp
    except ftplib.all_errors as e:
        if isinstance(e, (ftplib.error_perm, ftplib.error_temp)):
            raise _map_error(e) from e
        raise


def probe(url, timeout=30, ssl_context=None):
    p, _u, _pw, path = _parts(url)
    ftp = connect(url, timeout, ssl_context)
    try:
        try:
            total = ftp.size(path)
        except ftplib.error_perm as e:
            raise _map_error(e) from e
        resumable = False
        try:
            resumable = ftp.sendcmd("REST 0").startswith("350")
        except ftplib.all_errors:
            pass
        mdtm = None
        try:
            mdtm = ftp.sendcmd(f"MDTM {path}")[4:].strip()
        except ftplib.all_errors:
            pass
        name = os.path.basename(path.rstrip("/")) or "download"
        return {"final_url": url, "total": total, "resumable": resumable and total is not None,
                "name": name, "raw_name": name, "etag": None, "last_modified": mdtm,
                "type": "application/octet-stream"}
    finally:
        try:
            ftp.quit()
        except ftplib.all_errors:
            ftp.close()


class FtpResponse:
    status = 206
    reason = "Partial Content"

    def __init__(self, conn, sock, start, end, total):
        self.conn, self.sock = conn, sock
        self.start, self.end, self.total = start, end, total
        self.length = end - start + 1

    def getheader(self, name, default=None):
        if name.lower() == "content-range":
            return f"bytes {self.start}-{self.end}/{self.total}"
        return default

    def readinto(self, mv):
        if self.length <= 0:
            return 0
        n = self.sock.recv_into(mv[:min(len(mv), self.length)])
        if n == 0:
            return 0
        self.length -= n
        if self.length == 0:
            self._finish()
        return n

    def _finish(self):
        try:
            self.sock.close()
        except OSError:
            pass
        if self.end == self.total - 1:
            try:
                self.conn.ftp.voidresp()          # 226 Transfer complete → control reusable
                return
            except ftplib.all_errors:
                pass
        self.conn.close()                          # stopped mid-file: drop the control link

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class FtpConn:
    """Same interface as HttpConn.get_range for the segmented engine."""

    def __init__(self, d):
        self.d = d
        self.ftp = None

    def close(self):
        if self.ftp is not None:
            try:
                self.ftp.close()
            except Exception:  # noqa: BLE001
                pass
        self.ftp = None

    def get_range(self, url, start, end, validator=None):
        from .engine import ssl_context
        _p, _u, _pw, path = _parts(url)
        if self.ftp is None:
            self.ftp = connect(url, self.d._settings().get("timeout", 30), ssl_context())
        try:
            sock = self.ftp.transfercmd(f"RETR {path}", rest=start if start else None)
        except (ftplib.error_perm, ftplib.error_temp) as e:
            self.close()
            raise _map_error(e) from e
        except (OSError, EOFError, ftplib.Error):
            self.close()
            raise
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        return FtpResponse(self, sock, start, end, self.d.total), url
