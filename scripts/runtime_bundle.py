#!/usr/bin/env python3
"""Prepare, verify and restore dependency images outside Docker's build cache."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/runtime"))
import verify_runtime as verifier

POLICY = ROOT / "deploy/runtime/policy.json"
ROLES = ("tools", "mineru", "ollama")
ALIASES = {
    "tools": "catalogflow-tools-deps:py311-v1",
    "mineru": "catalogflow-mineru-deps:3.4.5-cpu-v1",
}
SHA = re.compile(r"[a-f0-9]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")


def command(args, *, capture=False, check=True, env=None, timeout=None):
    result = subprocess.run(
        args, cwd=ROOT, env=env, capture_output=capture, text=True, timeout=timeout
    )
    if check and result.returncode:
        raise ValueError(
            f"Command failed: {' '.join(args)}\n{(result.stderr or '')[-2000:]}"
        )
    return result


def image(reference):
    result = command(
        ["docker", "image", "inspect", reference], capture=True, check=False, timeout=20
    )
    if result.returncode:
        return None
    values = json.loads(result.stdout)
    return values[0] if values else None


def engine():
    result = command(
        ["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"],
        capture=True,
        timeout=15,
    )
    if result.stdout.strip() != "linux/amd64":
        raise ValueError("This runtime policy requires a Linux amd64 Docker engine")


def builder():
    env = os.environ.copy()
    result = command(
        ["docker", "buildx", "version"], capture=True, check=False, timeout=15
    )
    if result.returncode == 0:
        driver = command(
            ["docker", "buildx", "inspect", "default", "--format", "{{.Driver}}"],
            capture=True,
            timeout=20,
        )
        if driver.stdout.strip() != "docker":
            raise ValueError(
                "Offline rebuilds require the default Docker-driver builder so restored local images are visible"
            )
        return ["docker", "buildx", "build", "--builder", "default", "--load"], env
    # These Dockerfiles deliberately need no external frontend or BuildKit-only
    # mounts. Older installations without Buildx can use the engine builder.
    env.update(DOCKER_BUILDKIT="0", COMPOSE_BAKE="false")
    return ["docker", "build"], env


def build_image(dockerfile, tag, args=(), *, offline=False, no_cache=False):
    prefix, env = builder()
    options = ["--pull=false", "--file", str(dockerfile), "--tag", tag]
    if offline:
        options += ["--network", "none"]
    if no_cache:
        options += ["--no-cache"]
    for name, value in args:
        options += ["--build-arg", f"{name}={value}"]
    command(prefix + options + [str(ROOT)], env=env)


def sources():
    data = json.loads(
        command(
            ["docker", "compose", "config", "--format", "json"], capture=True
        ).stdout
    )
    return {
        "tools": data["services"]["pipeline-ui"]["image"],
        "mineru": data["services"]["mineru"]["image"],
        "ollama": data["services"]["local-llm"]["image"],
    }


def container_json(reference, role):
    name = "CatalogFlow_runtime_check_" + uuid.uuid4().hex[:12]
    result = command(
        [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "python",
            reference,
            "/opt/catalogflow/verify_runtime.py",
            "check",
            "--role",
            role,
            "--json",
        ],
        capture=True,
    )
    return json.loads(result.stdout)


def seal(reference, role, temporary):
    if image(reference) is None:
        raise ValueError(f"Dependency source image is unavailable: {reference}")
    dockerfile = temporary / f"{role}.Dockerfile"
    dockerfile.write_text(
        "ARG SOURCE_IMAGE\nFROM ${SOURCE_IMAGE}\nUSER 0:0\n"
        "ENV CATALOGFLOW_DEPENDENCY_SHA256=\n"
        "COPY deploy/runtime/verify_runtime.py deploy/runtime/policy.json /opt/catalogflow/\n"
        f"RUN python /opt/catalogflow/verify_runtime.py capture --role {role}\n"
    )
    print(
        f"Sealing {role}: checking versions and hashing libraries/assets once",
        flush=True,
    )
    build_image(dockerfile, ALIASES[role], [("SOURCE_IMAGE", reference)], offline=True)
    lock = container_json(ALIASES[role], role)
    lock_bytes = verifier.canonical(lock) + b"\n"
    lock_sha = hashlib.sha256(lock_bytes).hexdigest()
    tag = f"catalogflow-{role}-deps:{lock_sha[:16]}"
    command(["docker", "image", "tag", ALIASES[role], tag])
    return {"tag": tag, "id": image(tag)["Id"], "lock_sha256": lock_sha}, lock_bytes


def resolve(directory):
    if (directory / "manifest.json").is_file():
        return directory
    try:
        pointer = json.loads((directory / "current.json").read_text())["bundle"]
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(
            "No prepared runtime bundle. Run: sudo python3 scripts/runtime_bundle.py prepare"
        ) from exc
    if not re.fullmatch(r"[a-f0-9]{20}", pointer):
        raise ValueError("Invalid runtime bundle pointer")
    return directory / pointer


def verify_bundle(directory, *, archive=True):
    folder = resolve(directory)
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest.get("schema") != 1 or set(manifest.get("images", {})) != set(ROLES):
        raise ValueError("Unsupported/incomplete runtime bundle manifest")
    if manifest.get("policy_sha256") != verifier.digest(POLICY):
        raise ValueError(
            "Runtime policy changed. Prepare a new bundle with --refresh; existing bundles remain intact"
        )
    for role, details in manifest["images"].items():
        prefix = (
            "catalogflow-ollama-runtime"
            if role == "ollama"
            else f"catalogflow-{role}-deps"
        )
        if not IMAGE_ID.fullmatch(details.get("id", "")) or not re.fullmatch(
            prefix + r":[a-f0-9]{16}", details.get("tag", "")
        ):
            raise ValueError("Invalid runtime image identity")
        if role != "ollama":
            lock = folder / f"{role}.lock.json"
            if (
                not SHA.fullmatch(details.get("lock_sha256", ""))
                or verifier.digest(lock) != details["lock_sha256"]
            ):
                raise ValueError(f"{role} dependency lock checksum mismatch")
            data = json.loads(lock.read_text())
            if (
                data.get("policy_sha256") != manifest["policy_sha256"]
                or data.get("role") != role
            ):
                raise ValueError("Dependency lock does not match this bundle policy")
            verifier.check_policy(role, json.loads(POLICY.read_text()), data)
        elif details.get("version") != json.loads(POLICY.read_text())[
            "ollama_version"
        ] or not re.fullmatch(
            r"[a-z0-9][a-zA-Z0-9._/:@-]{0,254}", details.get("service_image", "")
        ):
            raise ValueError("Ollama image/version does not match the runtime policy")
    recorded = manifest["archive"]
    if recorded.get("file") != "images.tar.gz" or not SHA.fullmatch(
        recorded.get("sha256", "")
    ):
        raise ValueError("Invalid archive metadata")
    path = folder / "images.tar.gz"
    if archive and (
        not path.is_file()
        or path.stat().st_size != recorded["bytes"]
        or verifier.digest(path) != recorded["sha256"]
    ):
        raise ValueError(
            "Runtime archive checksum mismatch; restore a valid bundle before loading images"
        )
    identity = {
        "policy_sha256": manifest["policy_sha256"],
        "images": manifest["images"],
    }
    expected = hashlib.sha256(verifier.canonical(identity)).hexdigest()[:20]
    if manifest.get("bundle") != expected:
        raise ValueError("Bundle identity does not match its locked images")
    return folder, manifest


def prepare(directory, *, fresh=False, refresh=False):
    if (directory / "current.json").is_file() and not refresh and not fresh:
        folder, _ = verify_bundle(directory)
        print(f"Verified existing dependency bundle: {folder}")
        return folder
    engine()
    refs = sources()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".preparing-", dir=directory) as work:
        temporary = Path(work)
        images = {}
        for role in ("tools", "mineru"):
            reference = refs[role]
            if fresh or image(reference) is None:
                reference = f"catalogflow-{role}-deps:preparing"
                file = (
                    ROOT
                    / ("scripts" if role == "tools" else "mineru")
                    / "Dockerfile.dependencies"
                )
                print(
                    f"Preparing {role} dependencies from source; downloads are needed only for preparation",
                    flush=True,
                )
                args = []
                if role == "mineru":
                    source = os.getenv("MINERU_MODEL_SOURCE", "huggingface")
                    if source not in ("huggingface", "modelscope"):
                        raise ValueError(
                            "Fresh OCR downloads require MINERU_MODEL_SOURCE=huggingface or modelscope"
                        )
                    args = [("MINERU_MODEL_SOURCE", source)]
                build_image(file, reference, args)
            images[role], lock_bytes = seal(reference, role, temporary)
            (temporary / f"{role}.lock.json").write_bytes(lock_bytes)
        if image(refs["ollama"]) is None:
            command(["docker", "pull", refs["ollama"]])
        version = command(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                "CatalogFlow_runtime_version_" + uuid.uuid4().hex[:12],
                "--network",
                "none",
                "--entrypoint",
                "/bin/ollama",
                refs["ollama"],
                "--version",
            ],
            capture=True,
        )
        expected = json.loads(POLICY.read_text())["ollama_version"]
        if not re.search(
            r"\b" + re.escape(expected) + r"\b", version.stdout + (version.stderr or "")
        ):
            raise ValueError(f"Expected Ollama {expected}, got: {version.stdout}")
        ollama_id = image(refs["ollama"])["Id"]
        ollama_tag = (
            "catalogflow-ollama-runtime:" + ollama_id.removeprefix("sha256:")[:16]
        )
        command(["docker", "image", "tag", refs["ollama"], ollama_tag])
        images["ollama"] = {
            "tag": ollama_tag,
            "id": ollama_id,
            "version": expected,
            "service_image": refs["ollama"],
        }
        identity = {"policy_sha256": verifier.digest(POLICY), "images": images}
        bundle_id = hashlib.sha256(verifier.canonical(identity)).hexdigest()[:20]
        destination = directory / bundle_id
        if destination.exists():
            verify_bundle(destination)
        else:
            print(
                "Exporting dependency images (no catalogs, volumes or Qwen weights)",
                flush=True,
            )
            archive = temporary / "images.tar.gz"
            process = subprocess.Popen(
                ["docker", "image", "save", *[images[r]["tag"] for r in ROLES]],
                stdout=subprocess.PIPE,
            )
            try:
                with (
                    archive.open("wb") as raw,
                    gzip.GzipFile(
                        fileobj=raw, mode="wb", compresslevel=1, mtime=0
                    ) as compressed,
                ):
                    shutil.copyfileobj(process.stdout, compressed, length=1024 * 1024)
                if process.wait():
                    raise ValueError("Docker image export failed")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
            manifest = {
                "schema": 1,
                "bundle": bundle_id,
                **identity,
                "archive": {
                    "file": "images.tar.gz",
                    "bytes": archive.stat().st_size,
                    "sha256": verifier.digest(archive),
                },
            }
            (temporary / "manifest.json").write_bytes(
                verifier.canonical(manifest) + b"\n"
            )
            temporary.chmod(0o755)
            temporary.rename(destination)
        pointer = directory / (".current-" + uuid.uuid4().hex + ".json")
        pointer.write_text(json.dumps({"bundle": bundle_id}) + "\n")
        pointer.replace(directory / "current.json")
    print(f"Dependency bundle ready: {destination}")
    return destination


def restore(directory):
    folder, manifest = verify_bundle(directory, archive=False)
    engine()
    missing = any(
        (image(d["tag"]) or {}).get("Id") != d["id"]
        for d in manifest["images"].values()
    )
    if missing:
        verify_bundle(folder)  # Reject damaged archives before Docker loads them.
        command(["docker", "image", "load", "--input", str(folder / "images.tar.gz")])
    for role, details in manifest["images"].items():
        if (image(details["tag"]) or {}).get("Id") != details["id"]:
            raise ValueError(
                f"Restored {role} image ID differs from the dependency manifest"
            )
        if role != "ollama":
            lock = container_json(details["tag"], role)
            if (
                hashlib.sha256(verifier.canonical(lock) + b"\n").hexdigest()
                != details["lock_sha256"]
            ):
                raise ValueError(
                    f"Restored {role} version lock differs from the bundle"
                )
            command(["docker", "image", "tag", details["tag"], ALIASES[role]])
        else:
            if details["version"] != json.loads(POLICY.read_text())["ollama_version"]:
                raise ValueError("Ollama version differs from the runtime policy")
            command(
                ["docker", "image", "tag", details["tag"], details["service_image"]]
            )
    return manifest


def rebuild(directory, *, no_cache=False):
    if (
        not (directory / "manifest.json").is_file()
        and not (directory / "current.json").is_file()
    ):
        prepare(directory)
    refs = sources()
    _, locked = verify_bundle(directory, archive=False)
    if refs["ollama"] != locked["images"]["ollama"]["service_image"]:
        raise ValueError(
            "The configured Ollama image differs from the prepared bundle; prepare a matching bundle before rebuilding"
        )
    manifest = restore(directory)
    for role in ("tools", "mineru"):
        details = manifest["images"][role]
        variable = "TOOLS" if role == "tools" else "MINERU"
        file = ROOT / ("scripts" if role == "tools" else "mineru") / "Dockerfile"
        build_image(
            file,
            refs[role],
            [
                (f"{variable}_RUNTIME_IMAGE", details["tag"]),
                (f"{variable}_RUNTIME_LOCK_SHA", details["lock_sha256"]),
            ],
            offline=True,
            no_cache=no_cache,
        )
    print(
        "Application images rebuilt offline; existing catalogs and model storage were not changed"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["prepare", "verify", "restore", "build", "rebuild"]
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=Path(
            os.getenv("CATALOGFLOW_RUNTIME_DIR", str(ROOT / "artifacts/runtime"))
        ),
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="prepare dependencies from source instead of existing application images",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="prepare a new bundle while retaining previous versions",
    )
    args = parser.parse_args()
    directory = args.bundle.expanduser().resolve()
    if args.action == "prepare":
        prepare(directory, fresh=args.fresh, refresh=args.refresh)
    elif args.action == "verify":
        folder, manifest = verify_bundle(directory)
        print(f"Verified dependency bundle {manifest['bundle']}: {folder}")
    elif args.action == "restore":
        restore(directory)
    else:
        rebuild(directory, no_cache=args.action == "rebuild")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Runtime bundle error: {exc}")
