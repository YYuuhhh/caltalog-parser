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
