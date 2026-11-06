# Dependency bundles and clean offline rebuilds

The default CPU application images no longer install apt/Python packages or
download OCR models. Their Dockerfiles copy application files onto prepared,
version-checked runtime images. This also packages the scripts, UI and schemas
inside the tools image; source bind mounts remain available for development.

The dependency images are exported as ordinary host files under
`artifacts/runtime/`. They are separate from Docker's build cache and volumes.
Docker can restore them after its images/cache/volumes have been removed. Keep
this host directory, or place it on another disk with `--bundle /path/to/runtime`.
Deleting the bundle as well requires downloading/preparing dependencies again.

## Prepare once

Run inside the repository on the Linux amd64 server:

```bash
sudo python3 scripts/runtime_bundle.py prepare
```

Existing tools/MinerU images are reused when available: their installed versions
are checked, the lazy OCR imports and `pip check` are exercised, and libraries
and OCR assets are hashed once. No running container is committed or changed,
and no catalogs, runtime volumes or downloaded Qwen weights are included.
If an image is missing, its dependencies are built once using
`scripts/Dockerfile.dependencies` or `mineru/Dockerfile.dependencies`.

An old image with missing/wrong packages is rejected. Prepare from source instead:

```bash
sudo python3 scripts/runtime_bundle.py prepare --fresh
```

This initial preparation needs downloads and disk space for the archive. Later
application rebuilds do not rerun those installs. Preparation uses pinned CPU
PyTorch/MinerU/six constraints; it never selects the legacy CUDA target.

## Rebuild without Docker build cache

```bash
sudo python3 scripts/runtime_bundle.py rebuild
sudo docker compose up -d --no-build --pull never
```

`rebuild` restores missing dependency images, verifies image IDs and version
locks, then builds both application images with `--no-cache`, `--pull=false`
and `--network none`. It never downloads libraries/models, removes volumes,
stops containers or changes catalog/Qwen storage. Starting updated containers is
a separate step. Disk reads/imports still take time; there is no claimed
constant rebuild duration. If no bundle exists, first-time preparation runs.

For an incremental application build, use `build` instead of `rebuild`.
Make equivalents are `sudo make deps-prepare`, `sudo make deps-verify`,
`sudo make deps-restore`, `sudo make build`, and `sudo make rebuild`.
`make ui` and `make start` prepare/restore dependencies before building.

To keep the bundle outside the checkout:

```bash
sudo python3 scripts/runtime_bundle.py prepare --bundle /srv/catalogflow-runtime
sudo python3 scripts/runtime_bundle.py rebuild --bundle /srv/catalogflow-runtime
sudo make ui RUNTIME_BUNDLE=/srv/catalogflow-runtime
```

The script uses the default Docker-driver Buildx builder so imported images are
visible without registry requests. Older engines without Buildx use their
classic builder with Bake disabled for that invocation. Application Dockerfiles
have no remote `# syntax` frontend or BuildKit-only mounts. The optional legacy
GPU workflow has its own Dockerfile and is outside this CPU bundle.

## What is checked

Each content-identified bundle contains:

* `manifest.json`: image IDs/tags, policy hash, archive size and SHA-256.
* `tools.lock.json` and `mineru.lock.json`: the exact full installed Python
  package graph, Python patch version, system package versions, library-tree
  hashes, and OCR asset hashes/sizes.
* `images.tar.gz`: the two dependency images and the pinned Ollama image.

`current.json` selects a bundle. New bundles are written completely before this
pointer changes; previous versions remain available. A damaged archive is
rejected before Docker loads it. Already-present matching images are checked
without rereading the large archive on every incremental build.

```bash
sudo python3 scripts/runtime_bundle.py verify
sudo python3 scripts/runtime_bundle.py restore
```

Application builds and container startup compare installed package versions to
the sealed lock and enforce `policy.json`. MinerU model configuration and asset
sizes are checked; repeated startup/build checks do not rehash gigabytes of
weights. The immutable imported image IDs and verified archive protect the
sealed bytes. An optional full in-container content audit is available:

```bash
sudo docker compose run --rm --no-deps --name CatalogFlow_tools_dependency_audit tools \
  python /opt/catalogflow/verify_runtime.py check --role tools --deep
sudo docker compose run --rm --no-deps --name CatalogFlow_mineru_dependency_audit \
  --entrypoint python mineru /opt/catalogflow/verify_runtime.py check --role mineru --deep
```

Dependency policy/version mismatches fail with instructions; they do not trigger
silent `pip install` or an automatic model download at startup. Change the pins
in the dependency Dockerfiles, `policy.json` and `mineru.constraints.txt` together
when deliberately upgrading, then prepare a new bundle:

```bash
sudo python3 scripts/runtime_bundle.py prepare --fresh --refresh
sudo python3 scripts/runtime_bundle.py rebuild
```

The complete transitive version graph is frozen when the bundle is sealed.
Source preparation can resolve newer packages allowed by its requirements;
this produces a new lock/image identity. Ordinary rebuilds reuse the exact
sealed graph. MinerU's parser-cache fingerprint combines that dependency lock
with the current wrapper/entrypoint source, so app or dependency changes cannot
silently reuse incompatible parser caches.

Docker documents the underlying portable-image operations in
[image save](https://docs.docker.com/reference/cli/docker/image/save/) and
[image load](https://docs.docker.com/reference/cli/docker/image/load/).
