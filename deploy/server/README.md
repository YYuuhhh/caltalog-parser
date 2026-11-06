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
docker compose up -d --build
docker compose ps
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
docker compose up -d --build
```

If extraction fails with `ModuleNotFoundError: No module named 'six'`, the old
MinerU image is missing an OCR dependency. Repair the running container with:

```bash
sudo docker compose exec -T mineru python -m pip install 'six==1.17.0'
sudo docker compose restart mineru
```

After the service is healthy, use **Resume saved run** to retry failed pages.
The updated image installs `six` and verifies the OCR imports during its build.
Rebuild it to retain the fix when the container is recreated:

```bash
git pull --ff-only
sudo docker compose up -d --build mineru pipeline-ui
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
