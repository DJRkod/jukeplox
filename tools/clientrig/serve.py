"""Delivery and harness server: one component, two jobs the lanes both need.

**Delivery.** A canary is a patched copy of a file under `static/`, kept outside
the worktree so the repo is never mutated in place. This server overlays that
copy on the real tree and proxies everything else to the running instance. A
desktop browser points at it directly; the phone reaches it over
`adb reverse tcp:<port> tcp:<port>`. Without this there is no way to show a lane
failing against a canary, and the entire credentialing mechanism has nothing to
stand on.

**Clock precision.** WebKit clamps `performance.now()` to whole milliseconds
unless the document is cross-origin isolated, which swamps the effects being
measured. Isolation requires two response headers and is unreachable over a
`file://` URL -- which is how the existing perf bench loads its harness, and why
that harness cannot be reused for timing work as-is.

Three rules fall out of the two jobs:

*The page itself is proxied, never served from here.* Rendering our own HTML
would mean measuring a page the application never built. Only `static/` is
overlaid.

*A missing static file is a 404, not a proxy.* Falling through to upstream would
return the app's own error page with a body that looks like a working asset, so
a typo'd overlay path would read as a successfully-delivered canary.

*Isolation needs a resource policy on the same server.* `require-corp` blocks the
harness's own subresources -- album art included -- unless they are marked
same-origin. An isolated page that renders no art is not the page under test.

No upstream default. A rig points at a real deployment, and this repo's hygiene
suite forbids baking an address into anything under `tools/`.
"""
from __future__ import annotations

import http.server
import posixpath
import socketserver
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_STATIC_PREFIX = "/static/"

# Reproduction harnesses live outside `static/` because they are rig tooling,
# not app assets -- `tests/test_static_discipline.py` holds a closed allowlist of
# what may appear under `static/`, and a harness page does not belong on it.
# They still need an http origin rather than a file:// URL, since isolation (and
# therefore a usable clock) is unreachable over file://.
_HARNESS_PREFIX = "/harness/"

# Isolation headers. COOP + COEP together are what make `crossOriginIsolated`
# true, which is what lifts WebKit's 1ms clock clamp. CORP is on every response
# because COEP blocks subresources that do not carry it.
_ISOLATION = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
    "Cross-Origin-Resource-Policy": "same-origin",
}

_MIME = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class Resolution:
    """Where a request should be answered from.

    `kind` is one of: `overlay` (the canary's patched copy), `static` (the real
    tree), `proxy` (upstream), `missing` (a static path that exists nowhere), or
    `refused` (a path that tried to escape the roots).
    """

    kind: str
    path: Path | None = None
    detail: str = ""


def _safe_subpath(root: Path, rel: str) -> Path | None:
    """Join `rel` under `root`, or None if the request tried to escape it.

    Three layers, in this order, and the order matters:

    1. **Decode first.** Checking before decoding lets `..%2f..` straight
       through, since the dots only appear once the escape is resolved.
    2. **Reject any `..` segment outright, before normalising.** `normpath`
       would otherwise *absorb* the traversal -- `/../../etc/passwd` collapses
       to `/etc/passwd`, which then joins harmlessly inside the root and reports
       as a missing file. That is safe but dishonest: it turns an escape attempt
       into an ordinary 404, so nothing in a log or a test can tell the
       difference between a typo and an attack.
    3. **Confirm by resolved prefix.** Belt and braces, and the only layer that
       catches a symlink pointing out of the tree.
    """
    decoded = urllib.parse.unquote(rel)
    if "\x00" in decoded:
        return None

    segments = decoded.replace("\\", "/").split("/")
    if any(seg == ".." for seg in segments):
        return None

    normalised = posixpath.normpath("/" + decoded.replace("\\", "/")).lstrip("/")
    try:
        candidate = (root / normalised).resolve()
        candidate.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return candidate


def resolve(request_path: str, static_root: Path,
            overlay_root: Path | None,
            harness_root: Path | None = None) -> Resolution:
    """Decide where one request is answered from, without touching the network."""
    path_only = urllib.parse.urlsplit(request_path).path

    if path_only.startswith(_HARNESS_PREFIX):
        if harness_root is None:
            return Resolution("missing", detail="no harness root configured")
        hrel = path_only[len(_HARNESS_PREFIX):]
        hpath = _safe_subpath(Path(harness_root), hrel)
        if hpath is None:
            return Resolution("refused", detail="path escapes the harness root")
        if hpath.is_file():
            return Resolution("harness", hpath)
        return Resolution("missing",
                          detail=f"{path_only} is not a harness file")

    if not path_only.startswith(_STATIC_PREFIX):
        return Resolution("proxy", detail="not a static path")

    rel = path_only[len(_STATIC_PREFIX):]

    if overlay_root is not None:
        patched = _safe_subpath(Path(overlay_root), rel)
        if patched is None:
            return Resolution("refused", detail="path escapes the overlay root")
        if patched.is_file():
            return Resolution("overlay", patched)

    real = _safe_subpath(Path(static_root), rel)
    if real is None:
        return Resolution("refused", detail="path escapes the static root")
    if real.is_file():
        return Resolution("static", real)

    return Resolution(
        "missing",
        detail=(f"{path_only} exists in neither the overlay nor the real tree; "
                f"refusing to proxy it, because upstream would answer with its "
                f"own error page and a mistyped canary path would read as a "
                f"delivered one"))


def headers_for(request_path: str) -> dict:
    """Response headers for a path: isolation always, no-store for static.

    A canary swapped between arms must never be served from cache -- that is a
    silent measurement of the previous build.
    """
    hdrs = dict(_ISOLATION)
    path = urllib.parse.urlsplit(request_path).path
    if path.startswith(_STATIC_PREFIX) or path.startswith(_HARNESS_PREFIX):
        hdrs["Cache-Control"] = "no-store, must-revalidate"
    return hdrs


def _content_type(path: Path) -> str:
    return _MIME.get(path.suffix.lower(), "application/octet-stream")


class _Handler(http.server.BaseHTTPRequestHandler):
    # Set by make_server
    upstream = ""
    static_root: Path = None
    overlay_root: Path = None
    harness_root: Path = None

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):            # quiet by default
        pass

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        for k, v in headers_for(self.path).items():
            self.send_header(k, v)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):                             # noqa: N802 (stdlib contract)
        r = resolve(self.path, self.static_root, self.overlay_root,
                    self.harness_root)

        if r.kind == "refused":
            self._send(403, r.detail.encode("utf-8"), "text/plain; charset=utf-8")
            return
        if r.kind == "missing":
            self._send(404, r.detail.encode("utf-8"), "text/plain; charset=utf-8")
            return
        if r.kind in ("overlay", "static", "harness"):
            try:
                body = r.path.read_bytes()
            except OSError as exc:
                self._send(500, str(exc).encode("utf-8"),
                           "text/plain; charset=utf-8")
                return
            self._send(200, body, _content_type(r.path))
            return

        # proxy
        target = self.upstream.rstrip("/") + self.path
        try:
            req = urllib.request.Request(target, method="GET")
            with urllib.request.urlopen(req, timeout=30) as up:
                body = up.read()
                ctype = up.headers.get("Content-Type",
                                       "application/octet-stream")
                self._send(up.status, body, ctype)
        except Exception as exc:                  # upstream down or refused
            self._send(
                502,
                (f"upstream {target} did not answer: {exc}").encode("utf-8"),
                "text/plain; charset=utf-8")

    do_HEAD = do_GET


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """Stay quiet when a browser hangs up, loud about anything else.

        A closing browser resets its connections, which the stdlib logs as a
        full traceback on stderr. During a measurement run that noise is worse
        than useless: it buries real errors and, when a probe captures both
        streams, it corrupts the JSON the run is trying to emit. Anything that
        is not a normal disconnect still gets reported.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def make_server(upstream: str, overlay_root, static_root,
                harness_root=None, host: str = "127.0.0.1", port: int = 0):
    """Build (but do not start) the server. Returns `(server, bound_port)`.

    `upstream` is required and never defaulted: a rig points at a real
    deployment, so the target is stated every time. Bound to loopback by
    default -- the phone reaches it through `adb reverse`, which does not need
    the port exposed on the network.
    """
    if not (upstream or "").strip():
        raise ValueError(
            "no upstream target: this server proxies everything it does not "
            "overlay, so the target must be stated explicitly. There is "
            "deliberately no default -- a rig points at a real deployment")

    handler = type("_BoundHandler", (_Handler,), {
        "upstream": upstream.strip(),
        "static_root": Path(static_root),
        "overlay_root": Path(overlay_root) if overlay_root else None,
        "harness_root": Path(harness_root) if harness_root else None,
    })
    httpd = _Server((host, port), handler)
    return httpd, httpd.server_address[1]
