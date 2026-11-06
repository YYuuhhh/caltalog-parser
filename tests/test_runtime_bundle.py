"""Dependency integrity and cold rebuild contracts; no ML downloads or pruning."""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import runtime_bundle as bundle

verify = bundle.verifier


@pytest.fixture
def sealed_runtime(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "os": "linux",
                "python_minor": "3.11",
                "tools": {"required": ["fastapi"], "exact": {"fastapi": "0.162.0"}},
            }
        )
    )
    actual = {
        "python": "3.11.14",
        "platform": "linux",
        "architecture": "x86_64",
        "packages": {"fastapi": "0.162.0", "transitive": "1.0"},
        "system_packages": {"libc6": "2.36-9"},
    }
    library = tmp_path / "site-packages"
    library.mkdir()
    (library / "dependency.py").write_text("verified library bytes\n")
    lock = tmp_path / "dependencies.lock.json"
    monkeypatch.setattr(verify, "inventory", lambda: copy.deepcopy(actual))
    monkeypatch.setattr(verify.sysconfig, "get_path", lambda _: str(library))
    monkeypatch.setattr(
        verify.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0)
    )
    verify.capture("tools", policy, lock)
    return policy, lock, actual, library


def test_full_version_lock_checks_transitive_and_system_changes(sealed_runtime):
    policy, lock, actual, _ = sealed_runtime
    assert verify.verify("tools", policy, lock)["packages"] == actual["packages"]
    actual["packages"]["transitive"] = "2.0"
    with pytest.raises(ValueError, match="Installed packages"):
        verify.verify("tools", policy, lock)
    actual["packages"]["transitive"] = "1.0"
    actual["system_packages"]["libc6"] = "different"
    with pytest.raises(ValueError, match="Installed system_packages"):
        verify.verify("tools", policy, lock)


def test_deep_verification_detects_library_changes_without_version_change(
    sealed_runtime,
):
    policy, lock, _, library = sealed_runtime
    verify.verify("tools", policy, lock, deep=True)
    (library / "dependency.py").write_text("unrecorded library code\n")
    with pytest.raises(ValueError, match="Library content changed"):
        verify.verify("tools", policy, lock, deep=True)


def test_corrupt_lock_or_changed_policy_fails_closed(sealed_runtime):
    policy, lock, _, _ = sealed_runtime
    original = lock.read_bytes()
    lock.write_bytes(original + b" ")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify.verify("tools", policy, lock)
    lock.write_bytes(original)
    policy.write_text(policy.read_text() + " ")
    with pytest.raises(ValueError, match="policy changed"):
        verify.verify("tools", policy, lock)


def test_app_fingerprint_changes_with_code_or_lock_without_reading_library_tree(
    sealed_runtime,
):
    _, lock, _, library = sealed_runtime
    app = library.parent / "app.py"
    app.write_text("first application version\n")
    output = library.parent / "runtime-fingerprint.txt"
    verify.write_fingerprint(lock, [app], output)
    original = output.read_bytes()
    # Image integrity is checked on restore. App builds hash the small seal,
    # so accessing individual library/weight files is unnecessary here.
    (library / "dependency.py").unlink()
    verify.write_fingerprint(lock, [app], output)
    assert output.read_bytes() == original
    app.write_text("second application version\n")
    verify.write_fingerprint(lock, [app], output)
    assert output.read_bytes() != original
    code_changed = output.read_bytes()
    lock.write_bytes(lock.read_bytes() + b" ")
    verify.write_fingerprint(lock, [app], output)
    assert output.read_bytes() != code_changed


def test_asset_size_and_deep_hash_are_checked(sealed_runtime):
    policy, lock, _, library = sealed_runtime
    asset = library.parent / "model.bin"
    asset.write_bytes(b"model bytes")
    data = json.loads(lock.read_text())
    data["assets"] = [
        {
            "path": str(asset),
            "bytes": asset.stat().st_size,
            "sha256": verify.digest(asset),
        }
    ]
    lock.write_bytes(verify.canonical(data) + b"\n")
    lock.with_suffix(".sha256").write_text(verify.digest(lock))
    verify.verify("tools", policy, lock, deep=True)
    asset.write_bytes(b"wrong bytes")
    with pytest.raises(ValueError, match="asset checksum mismatch"):
        verify.verify("tools", policy, lock, deep=True)
    asset.unlink()
    with pytest.raises(ValueError, match="asset missing or changed"):
        verify.verify("tools", policy, lock)


def test_cpu_policy_rejects_cuda_torch_and_missing_six():
    policy = json.loads(bundle.POLICY.read_text())
    actual = {
        "python": "3.11.14",
        "platform": "linux",
        "architecture": "x86_64",
        "packages": {
            name: policy["mineru"]["exact"].get(name, "fixture")
            for name in policy["mineru"]["required"]
        },
    }
    verify.check_policy("mineru", policy, actual)
    actual["packages"]["torch"] = "2.9.1+cu128"
    with pytest.raises(ValueError, match="expected 2.9.1\\+cpu"):
        verify.check_policy("mineru", policy, actual)
    actual["packages"]["torch"] = "2.9.1+cpu"
    actual["packages"].pop("six")
    with pytest.raises(ValueError, match="Missing mineru dependency: six"):
        verify.check_policy("mineru", policy, actual)


class DockerFixture:
    """A disposable engine model; image archives contain metadata, never weights."""

    def __init__(self):
        self.calls, self.images = [], {}
        self.buildx, self.driver = True, "docker"
        self.refs = {
            "tools": "guhring-parser-tools:3",
            "mineru": "guhring-parser-mineru-cpu:3.4.5",
            "ollama": "ollama/ollama:0.32.5",
        }
        policy = json.loads(bundle.POLICY.read_text())
        self.locks = {}
        for role in ("tools", "mineru"):
            self.locks[role] = {
                "schema": 1,
                "role": role,
                "policy_sha256": verify.digest(bundle.POLICY),
                "python": "3.11.14",
                "platform": "linux",
                "architecture": "x86_64",
                "packages": {
                    name: policy[role]["exact"].get(name, "fixture")
                    for name in policy[role]["required"]
                },
                "system_packages": {},
                "libraries": [],
                "assets": [],
            }
        for role, ref in self.refs.items():
            self.images[ref] = {
                "Id": "sha256:" + hashlib.sha256(ref.encode()).hexdigest(),
                "lock": self.locks.get(role),
            }

    def command(self, args, **kwargs):
        self.calls.append((args, kwargs))
        code, output = 0, ""
        if args[1] == "version":
            output = "linux/amd64\n"
        elif args[1:3] == ["compose", "config"]:
            output = json.dumps(
                {
                    "services": {
                        name: {"image": self.refs[role]}
                        for name, role in [
                            ("pipeline-ui", "tools"),
                            ("mineru", "mineru"),
                            ("local-llm", "ollama"),
                        ]
                    }
                }
            )
        elif args[1:3] == ["buildx", "version"]:
            code = 0 if self.buildx else 1
        elif args[1:3] == ["buildx", "inspect"]:
            output = self.driver + "\n"
        elif args[1:3] == ["image", "inspect"]:
            value = self.images.get(args[3])
            code, output = (0, json.dumps([value])) if value else (1, "")
        elif args[1:3] == ["image", "tag"]:
            self.images[args[4]] = copy.deepcopy(self.images[args[3]])
        elif args[1:3] == ["image", "load"]:
            self.images.update(json.loads(gzip.decompress(Path(args[-1]).read_bytes())))
        elif args[1] == "run":
            assert "--network" in args and args[args.index("--network") + 1] == "none"
            assert args[args.index("--name") + 1].startswith("CatalogFlow_")
            if args[-1] == "--version":
                output = "ollama version is 0.32.5\n"
            else:
                ref = args[args.index("--entrypoint") + 2]
                output = json.dumps(self.images[ref]["lock"])
        elif args[1] == "build" or args[1:3] == ["buildx", "build"]:
            tag = args[args.index("--tag") + 1]
            role = "tools" if "tools" in tag else "mineru"
            self.images[tag] = {
                "Id": "sha256:" + hashlib.sha256((tag + "sealed").encode()).hexdigest(),
                "lock": copy.deepcopy(self.locks[role]),
            }
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, code, output, "")

    def save(self, args, **kwargs):
        assert args[:3] == ["docker", "image", "save"]
        self.calls.append((args, kwargs))
        body = json.dumps({ref: self.images[ref] for ref in args[3:]}).encode()

        class Process:
            stdout = io.BytesIO(body)

            def poll(self):
                return 0

            def wait(self):
                return 0

        return Process()


@pytest.fixture
def docker_bundle(tmp_path, monkeypatch):
    docker = DockerFixture()
    monkeypatch.setattr(bundle, "command", docker.command)
    monkeypatch.setattr(bundle.subprocess, "Popen", docker.save)
    directory = tmp_path / "external-runtime"
    prepared = bundle.prepare(directory)
    return docker, directory, prepared


def test_empty_engine_rebuild_restores_verified_bundle_without_downloads_or_cache(
    docker_bundle,
):
    docker, directory, prepared = docker_bundle
    assert bundle.verify_bundle(directory)[0] == prepared
    original = (directory / "current.json").read_bytes()
    docker.images.clear()  # Simulate a Docker image/cache/volume wipe.
    docker.calls.clear()
    bundle.rebuild(directory, no_cache=True)
    calls = [args for args, _ in docker.calls]
    loads = [args for args in calls if args[1:3] == ["image", "load"]]
    builds = [args for args in calls if args[1:3] == ["buildx", "build"]]
    assert len(loads) == 1 and len(builds) == 2
    assert calls.index(loads[0]) < calls.index(builds[0])
    for args in builds:
        assert "--no-cache" in args and "--pull=false" in args
        assert args[args.index("--network") + 1] == "none"
        assert not args[args.index("--file") + 1].endswith(".dependencies")
    assert not any(args[1] in ("pull", "volume") for args in calls)
    assert (directory / "current.json").read_bytes() == original


def test_corrupt_archive_is_rejected_before_docker_load(docker_bundle):
    docker, directory, prepared = docker_bundle
    archive = prepared / "images.tar.gz"
    data = bytearray(archive.read_bytes())
    data[-1] ^= 1
    archive.write_bytes(data)
    docker.images.clear()
    docker.calls.clear()
    with pytest.raises(ValueError, match="archive checksum mismatch"):
        bundle.restore(directory)
    assert not any(args[1:3] == ["image", "load"] for args, _ in docker.calls)


def test_installed_image_version_drift_is_rejected(docker_bundle):
    docker, directory, _ = docker_bundle
    _, manifest = bundle.verify_bundle(directory)
    tag = manifest["images"]["mineru"]["tag"]
    docker.images[tag]["lock"]["packages"]["torch"] = "wrong-version"
    with pytest.raises(ValueError, match="version lock differs"):
        bundle.restore(directory)


def test_buildx_absence_uses_legacy_engine_without_bake(docker_bundle):
    docker, directory, _ = docker_bundle
    docker.buildx = False
    docker.calls.clear()
    bundle.rebuild(directory, no_cache=True)
    builds = [(args, kwargs) for args, kwargs in docker.calls if args[1] == "build"]
    assert len(builds) == 2
    assert all(
        kw["env"]["DOCKER_BUILDKIT"] == "0" and kw["env"]["COMPOSE_BAKE"] == "false"
        for _, kw in builds
    )


def test_isolated_buildkit_store_is_not_silently_used(docker_bundle):
    docker, directory, _ = docker_bundle
    docker.driver = "docker-container"
    with pytest.raises(ValueError, match="Docker-driver"):
        bundle.rebuild(directory, no_cache=True)


def test_bundle_path_traversal_is_rejected(tmp_path):
    (tmp_path / "current.json").write_text('{"bundle":"../../outside"}')
    with pytest.raises(ValueError, match="Invalid runtime bundle pointer"):
        bundle.resolve(tmp_path)


def test_configured_ollama_change_requires_a_matching_bundle(docker_bundle):
    docker, directory, _ = docker_bundle
    docker.refs["ollama"] = "ollama/ollama:other-version"
    with pytest.raises(ValueError, match="configured Ollama image differs"):
        bundle.rebuild(directory, no_cache=True)


def test_cpu_constraints_match_the_dependency_policy():
    policy = json.loads(bundle.POLICY.read_text())
    pins = dict(
        line.split("==", 1)
        for line in (ROOT / "deploy/runtime/mineru.constraints.txt")
        .read_text()
        .splitlines()
        if line
    )
    assert pins == policy["mineru"]["exact"]


def test_default_services_preserve_runtime_verification_entrypoint():
    import yaml

    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name in ["init-storage", "pipeline-ui", "catalog-etl"]:
        assert "entrypoint" not in services[name]
        assert services[name]["command"][0] == "python"
    for name in ["init-storage", "pipeline-ui", "mineru"]:
        assert services[name]["build"]["context"] == "."
        assert services[name]["build"]["network"] == "none"
        assert services[name]["pull_policy"] == "never"


def test_application_dockerfiles_have_no_installs_or_external_frontend():
    for file in [ROOT / "scripts/Dockerfile", ROOT / "mineru/Dockerfile"]:
        text = file.read_text()
        assert "# syntax=" not in text
        assert (
            "apt-get" not in text
            and "pip install" not in text
            and "models-download" not in text
        )
        assert "verify_runtime.py" in text
        for line in text.splitlines():
            if line.startswith("COPY "):
                for source in line.split()[1:-1]:
                    assert (ROOT / source).exists(), source
