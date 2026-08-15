"""Shared HTTP + logging plumbing for the soak drivers.

admin_chaos.py, admin_toggles.py and monitor.py each talk to the same instance
in the same way. They used to carry their own copy of this, and the copies had
already drifted — different timeouts, different response-truncation limits, and
three separate definitions of "where is the target". A soak whose drivers
disagree about timeouts produces results that are hard to compare.

party.mjs already imports cdp.mjs on the JS side of this harness, so a shared
module is the established shape here.
"""

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 12
READ_LIMIT = 2000        # response bytes kept on success
ERROR_LIMIT = 500        # response bytes kept on an error status


def require_base() -> str:
    """The target must be stated explicitly.

    No default: a soak points at a real deployment, and a baked-in address is
    both a footgun and — on this project, which mirrors to a public repo — a
    leak of the validation rig's LAN address.
    """
    base = os.environ.get("JP_BASE", "").rstrip("/")
    if not base:
        sys.exit("JP_BASE is required, e.g. JP_BASE=http://jukebox.local")
    return base


def minutes(default: float = 30.0) -> float:
    return float(os.environ.get("JP_MINUTES", default))


class Recorder:
    """Line-buffered JSONL sink, one object per action."""

    def __init__(self, path: str):
        self._fh = open(path, "a", buffering=1, encoding="utf-8")

    def __call__(self, **obj):
        self._fh.write(json.dumps({"t": time.time(), **obj}) + "\n")

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


class Client:
    """Cookie-carrying HTTP client for one driver's session."""

    def __init__(self, base: str):
        self.base = base
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))

    def call(self, method: str, path: str, body=None, timeout: int = DEFAULT_TIMEOUT):
        """Return (status, body_text). Never raises: a soak driver that dies on
        a transport blip stops measuring the thing it was launched to measure.
        Status 0 means the request never completed."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.status, r.read(READ_LIMIT).decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read(ERROR_LIMIT).decode(errors="replace")
        except Exception as e:
            return 0, f"{type(e).__name__}: {e}"[:ERROR_LIMIT]

    def get_json(self, path: str, timeout: int = DEFAULT_TIMEOUT):
        """Parsed JSON, or {"_err": ...} — callers treat a read failure as a
        missing sample rather than a fatal error."""
        try:
            with self._opener.open(self.base + path, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            return {"_err": f"{type(e).__name__}: {e}"[:150]}

    def login(self, password: str):
        return self.call("POST", "/admin/auth/login/local", {"password": password})
