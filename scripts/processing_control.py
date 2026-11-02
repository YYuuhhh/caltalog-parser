"""Cooperative catalog checkpoints shared by the coordinator and CPU client."""

from __future__ import annotations

import json
import time
from pathlib import Path


def read(path):
    if path is None:
        return {}
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def checkpoint(control_file=None, queue_file=None):
    """Wait at a durable boundary; return seconds spent paused for timeout accounting."""
    acknowledgement = (
        Path(control_file).with_name("paused.json") if control_file else None
    )
    started = None
    try:
        while True:
            control = read(control_file)
            if control.get("stopped"):
                raise InterruptedError("Stopped by user.")
            if not (control.get("paused") or read(queue_file).get("paused")):
                return time.monotonic() - started if started is not None else 0
            if started is None:
                started = time.monotonic()
                if acknowledgement:
                    acknowledgement.write_text('{"paused": true}\n')
            time.sleep(0.1)
    finally:
        if started is not None and acknowledgement:
            acknowledgement.unlink(missing_ok=True)
