"""Seal and check a dependency runtime using Python's standard library only."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import sysconfig

ROOT = Path("/opt/catalogflow")
LOCK = ROOT / "dependencies.lock.json"
POLICY = ROOT / "policy.json"


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def normalized(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def packages():
    result = {}
    for distribution in importlib.metadata.distributions():
        name = normalized(distribution.metadata["Name"])
        if name in result and result[name] != distribution.version:
            raise ValueError(f"Conflicting installed versions of {name}")
        result[name] = distribution.version
    return dict(sorted(result.items()))


def system_packages():
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n"],
        check=True,
        capture_output=True,
        text=True,
    )
    return dict(sorted(line.split("\t", 1) for line in result.stdout.splitlines()))


def inventory():
    return {
        "python": platform.python_version(),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "packages": packages(),
        "system_packages": system_packages(),
    }


def check_policy(role, policy, actual):
    if actual["platform"] != policy["os"] or actual["architecture"] not in {
        "x86_64",
        "amd64",
    }:
        raise ValueError("The dependency bundle requires Linux amd64")
    if not actual["python"].startswith(policy["python_minor"] + "."):
        raise ValueError(
            f"Expected Python {policy['python_minor']}, got {actual['python']}"
        )
    for name in policy[role]["required"]:
        if normalized(name) not in actual["packages"]:
            raise ValueError(
                f"Missing {role} dependency: {name}. Prepare a fresh dependency bundle."
            )
    for name, version in policy[role]["exact"].items():
        installed = actual["packages"].get(normalized(name))
        if installed != version:
            raise ValueError(
                f"{name}: expected {version}, got {installed}. Prepare a fresh dependency bundle."
            )


def files(root, *, assets=False):
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and not p.name.endswith(".pyc")
        and (not assets or (".locks" not in p.parts and "logs" not in p.parts))
    )


def tree_record(root):
    result, count, size = hashlib.sha256(), 0, 0
    for path in files(root):
        length = path.stat().st_size
        result.update(canonical([str(path.relative_to(root)), length, digest(path)]))
        count += 1
        size += length
    return {
        "root": str(root),
        "sha256": result.hexdigest(),
        "files": count,
        "bytes": size,
    }


def asset_records(role):
    if role != "mineru":
        return []
    config = Path("/root/mineru.json")
    if not config.is_file():
        raise ValueError(
            "MinerU model configuration is missing; prepare the OCR assets first"
        )
    roots = [Path("/root/.cache/huggingface/hub"), Path("/root/.cache/modelscope/hub")]
    paths = [config] + [
        p for root in roots if root.exists() for p in files(root, assets=True)
    ]
    if not any(p.stat().st_size > 1024 * 1024 for p in paths):
        raise ValueError("No downloaded MinerU OCR model weights were found")
    return [
        {"path": str(p), "bytes": p.stat().st_size, "sha256": digest(p)} for p in paths
    ]


def capture(role, policy_path=POLICY, lock_path=LOCK):
    policy = json.loads(policy_path.read_text())
    actual = inventory()
    check_policy(role, policy, actual)
    # Resolve hidden lazy OCR imports before sealing, without loading a model.
    if role == "mineru":
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from mineru.model.utils.pytorchocr.data.imaug import operators",
            ],
            check=True,
        )
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    roots = sorted({Path(sysconfig.get_path(name)) for name in ("purelib", "platlib")})
    value = {
        "schema": 1,
        "role": role,
        "policy_sha256": digest(policy_path),
        **actual,
        "libraries": [tree_record(root) for root in roots],
        "assets": asset_records(role),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(canonical(value) + b"\n")
    lock_path.with_suffix(".sha256").write_text(digest(lock_path) + "\n")
    return value


def verify(role, policy_path=POLICY, lock_path=LOCK, expected=None, deep=False):
    expected = (
        expected
        or os.environ.get("CATALOGFLOW_DEPENDENCY_SHA256")
        or lock_path.with_suffix(".sha256").read_text().strip()
    )
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or digest(lock_path) != expected:
        raise ValueError(
            "Dependency lock checksum mismatch; restore the verified runtime bundle"
        )
    lock = json.loads(lock_path.read_text())
    if (
        lock.get("schema") != 1
        or lock.get("role") != role
        or lock.get("policy_sha256") != digest(policy_path)
    ):
        raise ValueError("Dependency policy changed; prepare a matching runtime bundle")
    actual = inventory()
    check_policy(role, json.loads(policy_path.read_text()), actual)
    for field, value in actual.items():
        if lock.get(field) != value:
            raise ValueError(
                f"Installed {field} differ from the sealed dependency lock; restore/rebuild dependencies"
            )
    for asset in lock["assets"]:
        path = Path(asset["path"])
        if not path.is_file() or path.stat().st_size != asset["bytes"]:
            raise ValueError(f"MinerU asset missing or changed: {path}")
        if (deep or path == Path("/root/mineru.json")) and digest(path) != asset[
            "sha256"
        ]:
            raise ValueError(f"MinerU asset checksum mismatch: {path}")
    if deep:
        for tree in lock["libraries"]:
            if tree_record(Path(tree["root"])) != tree:
                raise ValueError(f"Library content changed: {tree['root']}")
    return lock


def write_fingerprint(lock_path, app_files, output):
    output.write_bytes(
        canonical(
            {
                "dependencies": digest(lock_path),
                "app": {str(p): digest(p) for p in app_files},
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "check", "fingerprint"])
    parser.add_argument("--role", choices=["tools", "mineru"], required=True)
    parser.add_argument("--policy", type=Path, default=POLICY)
    parser.add_argument("--lock", type=Path, default=LOCK)
    parser.add_argument("--expected")
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "capture":
        value = capture(args.role, args.policy, args.lock)
    else:
        value = verify(args.role, args.policy, args.lock, args.expected, args.deep)
    if args.action == "fingerprint":
        if args.output is None:
            parser.error("--output is required for fingerprint")
        # Large library/weight hashes were sealed once, not reread on every app rebuild.
        write_fingerprint(
            args.lock, [Path("/app/app.py"), Path("/app/entrypoint.sh")], args.output
        )
    elif args.json:
        print(json.dumps(value, sort_keys=True))
    else:
        print(f"{args.role} dependencies {args.action}: {digest(args.lock)}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Dependency verification failed: {exc}")
