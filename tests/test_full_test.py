from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import full_test


def evidence(tmp_path):
    logs = tmp_path / "logs"
    parsed = tmp_path / "parsed"
    scripts = tmp_path / "scripts"
    root = logs / "full-test-runs/example"
    for path in [root, parsed, scripts]:
        path.mkdir(parents=True)
    raw = parsed / "master_raw.xlsx"
    raw.write_bytes(b"preserved RAW")
    wrapper = scripts / "app.py"
    wrapper.write_text("# wrapper")
    (root / "runtime.stdout").write_text(full_test.digest(wrapper))
    (root / "baseline.json").write_text(
        json.dumps(
            {
                "raw_sha256": full_test.digest(raw),
                "code_sha256": full_test.code_digests(scripts),
            }
        )
    )
    (root / "steps.tsv").write_text(
        "".join(
            f"{name}\t{'quality' if name == 'golden' else 'operational'}\t0\t1\n"
            for name in sorted(full_test.REQUIRED_STEPS)
        )
    )
    output = {
        "outputs": {"canonical.xlsx": {"content_sha256": "a" * 64}},
        "results": {"blocking_issues": 0},
    }
    (root / "structure.stdout").write_text(json.dumps(output))
    (root / "cached_structure.stdout").write_text(
        json.dumps({**output, "cache_hit": True})
    )
    (root / "resources.jsonl").write_text(
        json.dumps(
            {"Name": "mineru", "CPUPerc": "123.5%", "MemUsage": "256.25MiB / 16GiB"}
        )
        + "\n"
    )
    return root, {
        "logs_root": logs,
        "parsed_root": parsed,
        "scripts_root": scripts,
        "wrapper_path": wrapper,
    }


def test_complete_live_evidence_passes_and_records_docker_memory(tmp_path):
    _, paths = evidence(tmp_path)
    report = full_test.summarize("example", **paths)
    assert report["status"] == "passed" and all(report["checks"].values())
    assert report["resources"]["mineru"]["memory_bytes_peak_observed"] == 268697600
    assert report["resources"]["mineru"]["cpu_percent_peak_observed"] == 123.5
    assert json.loads((paths["logs_root"] / "full-test-latest.json").read_text())[
        "report_sha256"
    ] == full_test.digest(paths["logs_root"] / "full-test-runs/example/report.json")


@pytest.mark.parametrize(
    "damage",
    [
        "missing_step",
        "missing_baseline",
        "changed_raw",
        "changed_code",
        "empty_etl_outputs",
    ],
)
def test_incomplete_evidence_never_reports_a_pass(tmp_path, damage):
    root, paths = evidence(tmp_path)
    if damage == "missing_step":
        steps = root / "steps.tsv"
        steps.write_text(steps.read_text().replace("restart\toperational\t0\t1\n", ""))
    elif damage == "missing_baseline":
        (root / "baseline.json").unlink()
    elif damage == "changed_raw":
        (paths["parsed_root"] / "master_raw.xlsx").write_bytes(b"changed")
    elif damage == "changed_code":
        paths["wrapper_path"].write_text("# changed after testing")
    else:
        for name in ["structure", "cached_structure"]:
            (root / f"{name}.stdout").write_text(
                json.dumps({"outputs": {}, "cache_hit": True})
            )
    report = full_test.summarize("example", **paths)
    assert report["status"] == "failed" and report["operational_failures"]


def test_extraction_quality_failure_is_distinct_from_operational_failure(tmp_path):
    root, paths = evidence(tmp_path)
    steps = root / "steps.tsv"
    steps.write_text(
        steps.read_text().replace("golden\tquality\t0", "golden\tquality\t1")
    )
    report = full_test.summarize("example", **paths)
    assert report["status"] == "quality_blocked"
    assert report["quality_failures"] == ["golden"]
    assert report["operational_failures"] == []
