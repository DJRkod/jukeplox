"""Build-info module — surfaces the git SHA and build timestamp baked
into the docker image at build time, so the running container can
prove which version of the code it's actually executing.

The values come from `JUKEPLOX_GIT_SHA`, `JUKEPLOX_BUILD_TIME`, and
`JUKEPLOX_IMAGE_TAG` environment variables. The Dockerfile sets these
from build ARGs; pass them on the build command line with
`--build-arg GIT_SHA=$(git rev-parse --short HEAD)` etc.

When running outside Docker (dev mode), the env vars are absent and
the module reports "unknown" — that's the intended signal that this
is a non-image build.

Surfaced via:
- Startup log line (one INFO log on app boot, visible in `docker logs`)
- GET /api/version JSON endpoint (no auth — non-sensitive)
"""
from __future__ import annotations

import os

GIT_SHA: str = os.environ.get("JUKEPLOX_GIT_SHA", "unknown")
BUILD_TIME: str = os.environ.get("JUKEPLOX_BUILD_TIME", "unknown")
IMAGE_TAG: str = os.environ.get("JUKEPLOX_IMAGE_TAG", "unknown")


def as_dict() -> dict[str, str]:
    return {
        "git_sha": GIT_SHA,
        "build_time": BUILD_TIME,
        "image_tag": IMAGE_TAG,
    }


def as_log_line() -> str:
    return f"Jukeplox build: git_sha={GIT_SHA} build_time={BUILD_TIME} image_tag={IMAGE_TAG}"
