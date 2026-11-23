"""Phone notifications through ntfy (https://ntfy.sh or a self-hosted ntfy server).
Install the free ntfy app, subscribe to your private topic, done."""
import base64
import secrets
import threading
import urllib.parse
import urllib.request


def new_topic():
    return "pydm-" + secrets.token_urlsafe(12).replace("_", "").replace("-", "")[:16].lower()


def send(server, topic, title, message, priority="default", tags="", opener=None, timeout=10):
    if not topic:
        return False
    server = (server or "https://ntfy.sh").rstrip("/")
    if urllib.parse.urlsplit(server).scheme != "https" and not server.startswith(
            ("http://127.", "http://localhost")):
        return False                                 # never send over plain HTTP to the internet
    url = f"{server}/{urllib.parse.quote(topic)}"
    req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST", headers={
        "Title": title if title.isascii() else
        "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?=",
        "Priority": priority, "Tags": tags, "Content-Type": "text/plain; charset=utf-8"})
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


def send_async(*args, **kw):
    threading.Thread(target=send, args=args, kwargs=kw, daemon=True).start()
