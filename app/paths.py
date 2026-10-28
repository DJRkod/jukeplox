"""Package-anchored filesystem locations.

Every path the app serves or reads from its own tree resolves through this
module, anchored on ``__file__`` rather than the process working directory.

In the Docker image the working directory is ``/app`` and the ``app`` package
is imported from ``/app/app`` (uvicorn puts the working directory first on
``sys.path``, ahead of the wheel installed into site-packages), so ``ROOT``
is ``/app`` — identical to the CWD-relative paths the code used before. On a
native install the service may start from any directory; these paths still
point at the checkout.

This module must import nothing from ``app`` — ``app.assets`` imports it, and
a reverse edge would be a cycle.
"""
from __future__ import annotations

from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
STATIC_DIR: Path = ROOT / "static"
TEMPLATES_DIR: Path = ROOT / "app" / "templates"
CONFIG_DIR: Path = ROOT / "config"
