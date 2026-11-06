"""Linux resource measurements and conservative bottleneck hints; no root access."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False))
    temp.replace(path)


def counters(path):
    try:
        return {
            p[0].rstrip(":"): int(p[1])
            for row in path.read_text().splitlines()
            if len(p := row.split()) >= 2 and p[1].isdigit()
        }
    except OSError:
        return {}


def hardware(proc=Path("/proc"), cgroup=Path("/sys/fs/cgroup")):
    memory = counters(proc / "meminfo")
    total = memory.get("MemTotal", 0) * 1024
    limit = None
    try:
        value = (cgroup / "memory.max").read_text().strip()
        limit = int(value) if value.isdigit() else None
    except OSError:
        pass
    if limit and total:
        total = min(total, limit)
    cpu = "Unknown CPU"
    try:
        lines = (proc / "cpuinfo").read_text().splitlines()
        cpu = next(
            s.partition(":")[2].strip() for s in lines if s.startswith("model name")
        )
    except (OSError, StopIteration):
        pass
    logical = (
        len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else os.cpu_count() or 1
    )
    physical = set()
    try:
        for cpu_id in os.sched_getaffinity(0):
            root = Path(f"/sys/devices/system/cpu/cpu{cpu_id}/topology")
            physical.add(
                (
                    (root / "physical_package_id").read_text(),
                    (root / "core_id").read_text(),
                )
            )
    except (OSError, AttributeError):
        pass
    budget = float(logical)
    try:
        quota, period = (cgroup / "cpu.max").read_text().split()
        if quota != "max":
            budget = min(budget, int(quota) / int(period))
    except (OSError, ValueError):
        pass
    return {
        "cpu": cpu,
        "logical_cpus": logical,
        "cpu_budget": budget,
        "physical_cores": len(physical) or logical,
        "memory_bytes": total,
        "host_memory_bytes": memory.get("MemTotal", 0) * 1024,
        "memory_limit_bytes": limit,
        "scope": "Linux host counters; container limits reported separately",
    }


class Probe:
    def __init__(self, proc=Path("/proc"), cgroup=Path("/sys/fs/cgroup")):
        self.proc, self.cgroup, self.previous = proc, cgroup, None

    def sample(self):
        now = time.monotonic()
        mem = counters(self.proc / "meminfo")
        vm = counters(self.proc / "vmstat")
        ticks = []
        try:
            ticks = [
                int(v)
                for v in (self.proc / "stat").read_text().splitlines()[0].split()[1:9]
            ]
        except (OSError, ValueError, IndexError):
            pass
        current = {"time": now, "ticks": ticks, "vm": vm}
        previous, self.previous = self.previous, current
        value = {
            "sampled_at": time.time(),
            "cpu_percent": None,
            "iowait_percent": None,
            "memory_total_bytes": mem.get("MemTotal", 0) * 1024,
            "memory_available_bytes": mem.get("MemAvailable", 0) * 1024,
            "swap_total_bytes": mem.get("SwapTotal", 0) * 1024,
            "swap_used_bytes": max(0, mem.get("SwapTotal", 0) - mem.get("SwapFree", 0))
            * 1024,
            "swap_in_bytes_sec": None,
            "swap_out_bytes_sec": None,
            "swap_in_pages": vm.get("pswpin"),
            "swap_out_pages": vm.get("pswpout"),
        }
        if previous:
            elapsed = max(0.001, now - previous["time"])
            if len(ticks) == 8 and len(previous["ticks"]) == 8:
                delta = [max(0, a - b) for a, b in zip(ticks, previous["ticks"])]
                total = sum(delta)
                if total:
                    value["cpu_percent"] = round(
                        100 * (total - delta[3] - delta[4]) / total, 1
                    )
                    value["iowait_percent"] = round(100 * delta[4] / total, 1)
            for key, name in [
                ("pswpin", "swap_in_bytes_sec"),
                ("pswpout", "swap_out_bytes_sec"),
            ]:
                if key in vm and key in previous["vm"]:
                    value[name] = round(
                        max(0, vm[key] - previous["vm"][key])
                        * os.sysconf("SC_PAGE_SIZE")
                        / elapsed
                    )
        for source, name in [("cpu", "cpu"), ("memory", "memory"), ("io", "io")]:
            try:
                row = (
                    (self.proc / "pressure" / source)
                    .read_text()
                    .splitlines()[0]
                    .split()
                )
                value[name + "_pressure_percent"] = float(
                    dict(v.split("=") for v in row[1:])["avg10"]
                )
            except (OSError, ValueError, KeyError, IndexError):
                value[name + "_pressure_percent"] = None
        value["container_events"] = counters(self.cgroup / "memory.events")
        return value


def bottlenecks(sample):
    """Hints, not a claim to measure DRAM bandwidth or attribute host swap to a model."""
    findings = []
    if (sample.get("swap_in_bytes_sec") or 0) + (
        sample.get("swap_out_bytes_sec") or 0
    ) > 0:
        findings.append(
            {
                "kind": "swap",
                "text": "Active host swap I/O; memory is being paged to/from disk. Model-specific attribution is unavailable.",
            }
        )
    if (sample.get("iowait_percent") or 0) >= 10 or (
        sample.get("io_pressure_percent") or 0
    ) >= 10:
        findings.append(
            {
                "kind": "disk",
                "text": "Elevated host I/O wait/pressure; check model disk reads and competing disk work.",
            }
        )
    total = sample.get("memory_total_bytes") or 0
    if total and sample.get("memory_available_bytes", total) / total < 0.1:
        findings.append(
            {
                "kind": "memory",
                "text": "Less than 10% host RAM available; reduce model/context size or concurrent workloads.",
            }
        )
    if (sample.get("cpu_percent") or 0) >= 85:
        findings.append(
            {
                "kind": "cpu",
                "text": "High host CPU utilization; compute contention is a possible bottleneck.",
            }
        )
    if not findings:
        findings.append(
            {
                "kind": "undetermined",
                "text": "No measured saturation. Slow CPU inference can be limited by memory bandwidth; confirming that needs a hardware profiler.",
            }
        )
    return findings


class Monitor:
    def __init__(self, path, interval=2, model_probe=None):
        self.path, self.interval = Path(path), interval
        self.probe, self.stop_event, self.lock = (
            Probe(),
            threading.Event(),
            threading.RLock(),
        )
        self.model_probe, self.last_model_probe = model_probe, 0
        self.started, self.stage, self.data = (
            time.monotonic(),
            "starting",
            read_json(path, {}),
        )
        self.data.update(
            hardware=hardware(),
            scope="Host-wide resources; other applications can affect these measurements",
            live=True,
        )
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.model_thread = None

    def start(self):
        self.thread.start()
        return self

    def set_stage(self, stage):
        with self.lock:
            self.stage = stage

    def tick(self):
        sample = self.probe.sample()
        if (
            self.model_probe
            and time.monotonic() - self.last_model_probe >= 15
            and (self.model_thread is None or not self.model_thread.is_alive())
        ):
            self.last_model_probe = time.monotonic()
            self.model_thread = threading.Thread(target=self.probe_model, daemon=True)
            self.model_thread.start()
        with self.lock:
            self.data.update(
                current=sample,
                stage=self.stage,
                elapsed_seconds=round(time.monotonic() - self.started, 1),
                bottlenecks=bottlenecks(sample),
            )
            runner = self.data.get("inference_process", {})
            if runner.get("swap_bytes", 0) > 0:
                self.data["bottlenecks"].insert(
                    0,
                    {
                        "kind": "model_swap",
                        "text": f"Ollama processes have {runner['swap_bytes'] / 1024**2:.1f} MiB in swap (VmSwap). File-backed weight paging is separate.",
                    },
                )
            peak = self.data.setdefault("peak", {})
            stage = self.data.setdefault("stages", {}).setdefault(
                self.stage, {"samples": 0, "peak": {}}
            )
            stage["samples"] += 1
            for name, value in {
                "cpu_percent": sample["cpu_percent"],
                "iowait_percent": sample["iowait_percent"],
                "memory_used_bytes": sample["memory_total_bytes"]
                - sample["memory_available_bytes"],
                "swap_used_bytes": sample["swap_used_bytes"],
                "inference_rss_bytes": runner.get("rss_bytes"),
                "inference_swap_bytes": runner.get("swap_bytes"),
                "inference_cpu_cores": runner.get("cpu_cores_used"),
            }.items():
                if value is not None:
                    peak[name] = max(peak.get(name, 0), value)
                    stage["peak"][name] = max(stage["peak"].get(name, 0), value)
            atomic(self.path, self.data)
            with self.path.with_suffix(".jsonl").open("a") as history:
                history.write(json.dumps({"stage": self.stage, **sample}) + "\n")

    def probe_model(self):
        try:
            value = self.model_probe()
            result = {
                "inference_models": value.get("models", []),
                "inference_process": value.get("process", {}),
            }
            current = result["inference_process"]
            old = self.data.get("inference_process", {})
            elapsed = current.get("sampled_at", 0) - old.get("sampled_at", 0)
            if elapsed > 0 and old.get("available") and current.get("available"):
                before = {row["pid"]: row for row in old["processes"]}
                rows = [row for row in current["processes"] if row["pid"] in before]
                current["cpu_cores_used"] = round(
                    sum(
                        max(0, row["cpu_seconds"] - before[row["pid"]]["cpu_seconds"])
                        for row in rows
                    )
                    / elapsed,
                    2,
                )
                current["major_faults_per_second"] = round(
                    sum(
                        max(0, row["major_faults"] - before[row["pid"]]["major_faults"])
                        for row in rows
                    )
                    / elapsed,
                    2,
                )
        except Exception as exc:
            result = {"inference_error": str(exc)[:300]}
        with self.lock:
            if not self.stop_event.is_set():
                self.data.update(result)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except (OSError, ValueError):
                pass  # Diagnostics must never abort catalog processing.
            self.stop_event.wait(self.interval)

    def close(self):
        self.stop_event.set()
        self.thread.join(5)
        with self.lock:
            self.data["live"] = False
            try:
                atomic(self.path, self.data)
            except OSError:
                pass


def defaults(system, installed=()):
    gib = system["memory_bytes"] / 1024**3
    tier = 750 if gib >= 600 else 128 if gib >= 96 else 32
    preferred = "qwen3-vl:32b-thinking-bf16" if tier == 128 else "qwen3.5:9b"
    candidates = [
        m
        for m in installed
        if "medium" in m.get("reasoning_levels", [])
        and m.get("weights_gb", 0) < gib * 0.7
    ]
    if any(m["id"] == preferred for m in candidates):
        selected = preferred
    else:
        selected = (
            max(candidates, key=lambda m: m.get("weights_gb", 0))["id"]
            if candidates
            else preferred
        )
    return {
        "model": selected,
        "final_model": selected,
        "reasoning": "medium",
        "final_reasoning": "medium",
        "local_ram_gb": tier,
        "local_temperature": 0,
        "local_context_tokens": 24576 if tier >= 128 else 16384,
        "local_output_tokens": 4096,
        "review_timeout": 7200,
        "review_dpi": 144,
        "review_batch_rows": 20,
        "local_cpu_threads": max(
            1, min(system["physical_cores"], int(system["cpu_budget"]))
        ),
    }
