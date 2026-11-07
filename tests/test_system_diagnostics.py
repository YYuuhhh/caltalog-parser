"""Resource counters, host-vs-model attribution, and memory-based CPU defaults."""

from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import system_diagnostics as metrics
import inference_monitor


def test_deltas_distinguish_existing_swap_from_active_paging(tmp_path):
    (tmp_path / "meminfo").write_text(
        "MemTotal: 100000 kB\nMemAvailable: 50000 kB\nSwapTotal: 10000 kB\nSwapFree: 9000 kB\n"
    )
    (tmp_path / "stat").write_text("cpu 10 0 10 80 0 0 0 0\n")
    (tmp_path / "vmstat").write_text("pswpin 100\npswpout 200\n")
    probe = metrics.Probe(tmp_path, tmp_path / "no-cgroup")
    first = probe.sample()
    assert first["swap_used_bytes"] == 1000 * 1024
    assert first["swap_out_bytes_sec"] is None
    second = probe.sample()
    assert second["swap_out_bytes_sec"] == 0
    assert not any(f["kind"] == "swap" for f in metrics.bottlenecks(second))
    (tmp_path / "stat").write_text("cpu 40 0 30 120 10 0 0 0\n")
    (tmp_path / "vmstat").write_text("pswpin 102\npswpout 204\n")
    third = probe.sample()
    assert third["cpu_percent"] == 50 and third["iowait_percent"] == 10
    assert third["swap_in_bytes_sec"] > 0 and third["swap_out_bytes_sec"] > 0
    findings = metrics.bottlenecks(third)
    assert {f["kind"] for f in findings} >= {"swap", "disk"}
    assert "attribution is unavailable" in findings[0]["text"]


def test_missing_counters_are_unknown_not_false_zero_utilization(tmp_path):
    value = metrics.Probe(tmp_path, tmp_path).sample()
    assert value["cpu_percent"] is None and value["swap_in_bytes_sec"] is None
    assert metrics.bottlenecks(value)[0]["kind"] == "undetermined"


def test_server_defaults_use_quantized_review_pair_and_keep_reasoning_on():
    system = {"memory_bytes": 117 * 1024**3, "physical_cores": 24, "cpu_budget": 48}
    config = metrics.defaults(system)
    assert config["local_ram_gb"] == 128
    assert config["model"] == "qwen3-vl:8b-thinking-q4_K_M"
    assert config["final_model"] == "qwen3-vl:32b-thinking-q4_K_M"
    assert config["local_cpu_threads"] == 24
    assert config["local_temperature"] == 0
    assert config["reasoning"] == config["final_reasoning"] == "medium"
    small = metrics.defaults({**system, "memory_bytes": 32 * 1024**3, "cpu_budget": 4})
    assert small["local_ram_gb"] == 32 and small["local_cpu_threads"] == 4
    assert small["model"] == "qwen3.5:9b"
    installed = [
        {
            "id": "qwen3-vl:32b-thinking-bf16",
            "weights_gb": 66.7,
            "reasoning_levels": ["medium"],
        }
    ]
    assert metrics.defaults(system, installed)["model"] == config["model"]
    assert (
        metrics.defaults({**system, "physical_cores": None})["local_cpu_threads"] == 0
    )
    assert metrics.defaults({**system, "virtualized": True})["local_cpu_threads"] == 0


def test_cpuinfo_fallback_distinguishes_smt_from_physical_cores(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics.os, "sched_getaffinity", lambda _: {0, 1, 2, 3})
    (tmp_path / "cpuinfo").write_text(
        "\n\n".join(
            f"processor : {i}\nmodel name : Test CPU\nphysical id : 0\ncore id : {i // 2}\nflags : avx2 hypervisor"
            for i in range(4)
        )
    )
    value = metrics.hardware(tmp_path, tmp_path / "cgroup", tmp_path / "sysfs")
    assert value["logical_cpus"] == 4 and value["physical_cores"] == 2
    assert value["virtualized"] and value["cpu_topology_source"] == "cpuinfo"
    (tmp_path / "cpuinfo").write_text("processor : 0\nmodel name : Unknown topology\n")
    value = metrics.hardware(tmp_path, tmp_path / "cgroup", tmp_path / "sysfs")
    assert value["physical_cores"] is None and value["cpu_topology_source"] == "unknown"


def test_monitor_retains_peaks_stage_samples_and_stops_cleanly(tmp_path):
    monitor = metrics.Monitor(tmp_path / "performance.json", interval=0.02).start()
    monitor.set_stage("parse")
    time.sleep(0.08)
    monitor.close()
    saved = metrics.read_json(tmp_path / "performance.json")
    assert saved["live"] is False and saved["stages"]["parse"]["samples"] > 0
    assert (
        saved["peak"]["memory_used_bytes"]
        >= saved["current"]["memory_total_bytes"]
        - saved["current"]["memory_available_bytes"]
    )
    assert (tmp_path / "performance.jsonl").is_file()


def test_inference_namespace_reports_real_process_swap_without_host_attribution(
    tmp_path,
):
    runner = tmp_path / "100"
    runner.mkdir()
    (runner / "status").write_text(
        "Name:\tollama\nVmRSS:\t1024 kB\nVmHWM:\t2048 kB\nVmSwap:\t128 kB\n"
    )
    values = ["S"] + ["0"] * 25
    values[9], values[11], values[12] = "3", "100", "50"
    (runner / "stat").write_text("100 (ollama) " + " ".join(values))
    result = inference_monitor.processes(tmp_path)
    assert result["available"] and result["rss_bytes"] == 1024**2
    assert (
        result["peak_rss_bytes"] == 2 * 1024**2 and result["swap_bytes"] == 128 * 1024
    )
    assert result["major_faults"] == 3
    assert result["processes"][0]["name"] == "ollama"


def test_runner_cpu_fault_rates_and_swap_peaks_remain_separate_from_host(tmp_path):
    samples = iter(
        [
            {
                "available": True,
                "sampled_at": 100,
                "rss_bytes": 1000,
                "peak_rss_bytes": 2000,
                "swap_bytes": 1024,
                "processes": [{"pid": 5, "cpu_seconds": 10, "major_faults": 20}],
            },
            {
                "available": True,
                "sampled_at": 102,
                "rss_bytes": 1200,
                "peak_rss_bytes": 2200,
                "swap_bytes": 2048,
                "processes": [{"pid": 5, "cpu_seconds": 16, "major_faults": 24}],
            },
        ]
    )
    monitor = metrics.Monitor(
        tmp_path / "performance.json",
        model_probe=lambda: {"models": [], "process": next(samples)},
    )
    monitor.probe_model()
    monitor.probe_model()
    value = monitor.data["inference_process"]
    assert value["cpu_cores_used"] == 3 and value["major_faults_per_second"] == 2
    monitor.model_probe = None
    monitor.tick()
    assert monitor.data["peak"]["inference_rss_bytes"] == 1200
    assert monitor.data["peak"]["inference_swap_bytes"] == 2048
    assert monitor.data["bottlenecks"][0]["kind"] == "model_swap"
