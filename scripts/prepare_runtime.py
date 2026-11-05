"""Prepare writable bind-mount directories before the unprivileged UI starts."""

from __future__ import annotations

import os
from pathlib import Path
import stat


def prepare(roots, uid, gid):
    if not all(0 <= value < 2**32 - 1 for value in (uid, gid)):
        raise ValueError("LOCAL_UID and LOCAL_GID must be non-negative numeric IDs.")
    catalogs, logs, parsed = (
        Path(roots[name]) for name in ("catalogs", "logs", "parsed")
    )
    directories = [
        catalogs,
        logs,
        parsed,
        catalogs / "input",
        catalogs / "uploads",
        catalogs / "library",
        catalogs / "library/detections",
        catalogs / "library/mappings",
        catalogs / "library/mappings/history",
        catalogs / "library/workbooks",
        logs / "ui-runs",
        logs / "review-queue",
        logs / "local-model-installs",
        parsed / "ui-runs",
    ]

    def allow_owner(path, permissions):
        if path.is_symlink():
            raise ValueError(f"Runtime storage path must not be a symlink: {path}")
        info = path.stat()
        if (info.st_uid, info.st_gid) != (uid, gid):
            os.chown(path, uid, gid)
        mode = stat.S_IMODE(info.st_mode)
        if mode | permissions != mode:
            path.chmod(mode | permissions)

    for path in directories:
        if path.is_symlink():
            raise ValueError(f"Runtime storage path must not be a symlink: {path}")
        path.mkdir(parents=True, exist_ok=True)
        allow_owner(path, stat.S_IRWXU)
    # Repair the database and its SQLite companions without walking PDF inputs,
    # old extraction caches or model weights during every startup.
    for name in (
        "library.sqlite",
        "library.sqlite-wal",
        "library.sqlite-shm",
        "library.sqlite-journal",
    ):
        path = catalogs / "library" / name
        if path.exists() or path.is_symlink():
            allow_owner(path, stat.S_IRUSR | stat.S_IWUSR)


if __name__ == "__main__":
    roots = {
        name: Path(os.getenv(f"PIPELINE_{name.upper()}_ROOT", f"/{name}"))
        for name in ("catalogs", "logs", "parsed")
    }
    uid, gid = int(os.getenv("LOCAL_UID", "1000")), int(os.getenv("LOCAL_GID", "1000"))
    prepare(roots, uid, gid)
    print(f"Prepared CatalogFlow runtime storage for UID {uid}:GID {gid}.")
