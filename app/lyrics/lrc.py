"""Pure LRC (synced-lyrics) parser — no I/O, cheaply unit-tested.

LRC lines look like ``[mm:ss.cs]text`` (centiseconds) or ``[mm:ss.mmm]text``
(milliseconds); a line may carry several timestamps, and metadata tags like
``[ar:Artist]`` / ``[ti:Title]`` are not timestamps. We parse only digit:digit
timestamps, attach the line text that follows the last timestamp, and emit one
entry per timestamp, sorted by time.
"""
import re

# A timestamp tag: [m(m):ss(.frac)] — minutes 1-2 digits, seconds 2 digits,
# optional fractional part (centiseconds or milliseconds). Metadata tags like
# [ar:...] don't match because the first group requires digits.
_TS = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text: str | None) -> list[dict]:
    """Parse an LRC string into ordered ``[{"t_ms": int, "line": str}]``.

    Tolerates multiple timestamps per line, metadata tags (skipped), blank
    lines, and malformed lines (skipped). Fractional part is read left-aligned
    as milliseconds so 2-digit centiseconds and 3-digit milliseconds both work
    (``34`` → 340ms, ``345`` → 345ms)."""
    if not text:
        return []
    out: list[dict] = []
    for raw in text.splitlines():
        stamps = list(_TS.finditer(raw))
        if not stamps:
            continue
        line = raw[stamps[-1].end():].strip()
        for m in stamps:
            mm, ss = int(m.group(1)), int(m.group(2))
            frac = m.group(3) or ""
            ms = int(frac.ljust(3, "0")[:3]) if frac else 0
            out.append({"t_ms": (mm * 60 + ss) * 1000 + ms, "line": line})
    out.sort(key=lambda e: e["t_ms"])
    return out
