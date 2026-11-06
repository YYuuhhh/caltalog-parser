#!/usr/bin/env python3
"""Read-only Ollama process counters in its shared PID namespace; no Docker socket."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import time


def processes(proc=Path("/proc")):
    rows = []
    for folder in proc.iterdir():
        if not folder.name.isdigit():
            continue
        try:
            status = {
                k: v.strip()
                for line in (folder / "status").read_text().splitlines()
                if ":" in line
                for k, v in [line.split(":", 1)]
            }
            name = status.get("Name", "").lower()
            if "ollama" not in name and "llama" not in name:
                continue
            stat = (folder / "stat").read_text().rpartition(")")[2].split()
            value = lambda key: int(status.get(key, "0").split()[0]) * 1024
            rows.append(
                {
                    "pid": int(folder.name),
                    "name": name,
                    "rss_bytes": value("VmRSS"),
                    "peak_rss_bytes": value("VmHWM"),
                    "swap_bytes": value("VmSwap"),
                    "cpu_seconds": (int(stat[11]) + int(stat[12]))
                    / os.sysconf("SC_CLK_TCK"),
                    "major_faults": int(stat[9]),
                }
            )
        except (OSError, ValueError, IndexError):
            continue
    return {
        "available": bool(rows),
        "scope": "Ollama process namespace; CPU runner and server, excludes other services",
        "sampled_at": time.time(),
        "processes": rows,
        **{
            key: sum(row[key] for row in rows)
            for key in (
                "rss_bytes",
                "peak_rss_bytes",
                "swap_bytes",
                "cpu_seconds",
                "major_faults",
            )
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path not in ("/health", "/metrics"):
            self.send_error(404)
            return
        data = {"status": "healthy"} if self.path == "/health" else processes()
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8096), Handler).serve_forever()
