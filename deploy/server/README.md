# CatalogFlow on 172.172.1.25:7777

Run these commands on the Linux server with Git and Docker Compose installed.
They deploy the full CPU pipeline and web UI, including the model installation
menus. Port 7777 serves the web application; the model server uses its Docker
service address, `http://local-llm:11434`.

```bash
git clone https://github.com/YYuuhhh/caltalog-parser.git catalogflow
cd catalogflow
cp deploy/server/.env.example .env
sed -i "s/^LOCAL_UID=.*/LOCAL_UID=$(id -u)/;s/^LOCAL_GID=.*/LOCAL_GID=$(id -g)/" .env
mkdir -p catalogs/input catalogs/uploads catalogs/imports/guhring catalogs/library parsed logs
sudo python3 scripts/runtime_bundle.py prepare
sudo python3 scripts/runtime_bundle.py build
sudo docker compose up -d --no-build --pull never
sudo docker compose ps
```

`CatalogFlow_init-storage` runs once before the UI starts. It prepares the
writable runtime directories for `LOCAL_UID:LOCAL_GID`, including when Docker
created the bind-mount folders as root. The UI health check verifies storage and
library access instead of reporting a healthy process with a broken library.

For an existing installation showing `Permission denied: /catalogs/library`,
use the UID:GID reported by `sudo docker compose exec -T pipeline-ui id`:

```bash
# The default container identity is 1000:1000.
sudo chown -R 1000:1000 catalogs logs parsed
sudo docker compose run --rm --no-deps --name CatalogFlow_storage-repair init-storage
sudo docker compose up -d --wait pipeline-ui
```

Reload the browser after permissions are repaired. Future startups prepare the
runtime directories automatically. The initializer does not recursively scan
catalog originals, extraction caches or model weights.

Open **http://172.172.1.25:7777** from your browser. The server profile publishes
`0.0.0.0:7777` to port 7861 inside the UI container. The internal port and health
check stay at 7861. The extractor and Ollama host ports keep their loopback binds.
Containers retain their `CatalogFlow_` names, and data persists in the repository's
`catalogs/`, `parsed/`, `logs/` and `data/` directories.
The workbench has no login; make port 7777 reachable only by your trusted clients
or through an existing authenticated proxy.

The first build downloads MinerU's CPU extraction dependencies and OCR assets.
The Ollama server starts without LLM weights. In the UI, select the RAM preset
for this server and use **Install** next to a model, or select **No model** in
both menus to extract and structure catalogs while leaving model checks skipped
and the catalog unvalidated. No Work worker starts by default.

Upload PDFs through the web UI, or set `GUHRING_CATALOG_DIR` in `.env` to an
existing directory on this server before recreating the stack.

Check startup and application health:

```bash
docker compose logs --tail=100 mineru local-llm pipeline-ui
curl -fsS http://127.0.0.1:7777/health
```

To update an existing checkout, retain its `.env` and runtime data:

```bash
git pull --ff-only
sudo python3 scripts/runtime_bundle.py build
sudo docker compose up -d --no-build --pull never
```

Older MinerU images missing `six` cannot be sealed as valid dependencies. Prepare
a new runtime once, then rebuild the application:

```bash
sudo python3 scripts/runtime_bundle.py prepare --fresh --refresh
sudo python3 scripts/runtime_bundle.py rebuild
sudo docker compose up -d --no-build --pull never mineru pipeline-ui
```

Parser HTTP failures now retain the response details in the page manifest and a
bounded `error-response.json` beside it. The stoppage reason includes the failed
page and parser error. Detailed upstream tracebacks remain in
`logs/mineru-upstream.log`; a passing `/health` response from older images does
not prove the OCR dependencies can load.

## Slow model loading from HDD

The model server now defaults to `OLLAMA_LOAD_TIMEOUT=2h`. Apply updated Compose
settings without rebuilding images or deleting weights:

```bash
git pull --ff-only
sudo docker compose up -d --force-recreate local-llm pipeline-ui
```

The UI defaults to preloading while parsing, loading weights fully into CPU RAM,
and keeping weights resident. Model loading gets its own 7200-second budget;
each review response gets its full response timeout after loading. For an
existing saved run, these new options receive the defaults on Resume. To change
them explicitly, use Reuse settings and start with the updated settings.
The model-loading card reports elapsed time, readiness or the failure reason.

## Empty or invalid local review JSON

The Qwen3-VL thinking-Off compatibility fix and response diagnostics require an
application image update. Keep the running Ollama container and its loaded model:

```bash
git pull --ff-only
sudo python3 scripts/runtime_bundle.py build
sudo docker compose up -d --no-deps --no-build --pull never pipeline-ui
```

Then select **Resume** on the failed catalog. Completed extraction is reused;
the failed review gets a fresh task. No model weights need to be downloaded.
If this is the first update with runtime bundles, run
`sudo python3 scripts/runtime_bundle.py prepare` before `build` once.

For any new failure, open its stoppage dropdown for the error, token counts and
answer/thinking lengths. The complete original model response is saved at
`logs/review-queue/<task-id>/ollama-response.json`, including when JSON parsing or
validation fails. HTTP errors retain a bounded body in `ollama-http.json`.
The compatibility path accepts only a whole schema-valid result for the current
task, with thinking Off and Qwen3-VL; incomplete or unrelated answers remain failed.

## System defaults, diagnostics and library

New runs use hardware-selected CPU defaults. The reported 117 GiB server selects
the 128 GB profile with Qwen3-VL 32B Thinking BF16, temperature 0, thinking on,
20 RAW cells per review task, 144 DPI, 24576 context tokens and a 7200-second
response budget. CPU threads follow affinity/quota and physical-core detection.
Existing saved runs keep their parameters; use a new run or Reuse settings to
change them. The app does not change system swap configuration.

The stage performance dropdown records CPU/RAM peaks, active swap I/O, I/O wait,
first-token latency and model token rates. Stream chunk rates are estimates;
completed response rates use actual Ollama counters. Counters describe the host,
not exclusively the model. The read-only `inference-monitor` container shares
Ollama's PID namespace to report actual RSS/peak RSS, VmSwap, CPU core use and
major faults; it uses no Docker socket or host PID access. Host swap traffic and
file-backed paging remain separate measurements. A memory-bandwidth bottleneck cannot be confirmed
from these counters alone. Samples remain in `logs/ui-runs/<run-id>/performance.*`.

To add the monitor to an existing running stack while retaining Ollama:

```bash
git pull --ff-only
sudo python3 scripts/runtime_bundle.py build
sudo docker compose up -d --no-deps --no-build --pull never inference-monitor pipeline-ui
```

The bottom-left library shortcut includes unparsed PDFs and technical run history.
For the optional pinned CPU OCR model, follow
[Baidu Unlimited-OCR CPU setup](../baidu-cpu/README.md). It has a separate dependency
archive and persistent weights; the default stack does not start or download it.

For a different inference host, set `INFERENCE_MONITOR_URL` and
`INFERENCE_MONITOR_OLLAMA_URL` together in `.env`. Process counters are used only
when the selected Ollama URL matches that configured target; host-wide counters
always describe the application host.

## Full rebuild after removing Docker cache/images/volumes

Keep the ordinary host files in `artifacts/runtime/` (or your chosen external
bundle directory). They contain the dependency images and complete version locks;
Docker cleanup does not remove them. Then run:

```bash
sudo python3 scripts/runtime_bundle.py rebuild
sudo docker compose up -d --no-build --pull never
```

This verifies archive checksums before restoring missing images and rebuilds only
the small application layers with no cache and no build networking. Library/OCR
asset downloads occur only when initially preparing or explicitly upgrading the
bundle. Docker still needs to read/import the archive after an image wipe.
Deleting the host bundle too requires downloading dependencies again.

[Bundle storage, checks and upgrade instructions](../runtime/README.md).
