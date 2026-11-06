# Large server: CPU inference container, without weights

This standalone container processes the page/drawing evidence sent by CatalogFlow's
review and final-validation stages. The PDF extractor and workbench remain in the
main stack. It targets a Linux x86-64 server with Docker Compose. No GPU runtime,
CUDA/MLX/Vulkan backends, model weights, paid API calls or startup downloads are
included. A fresh server starts healthy with an empty model list.
The container is named `CatalogFlow_large-cpu`.

## Move the prepared container to the server

The generated `dist/catalogflow-large-cpu/` folder contains the image archive and
these deployment files. Copy that folder to the server, then run inside it:

```bash
sha256sum -c SHA256SUMS
gzip -dc catalogflow-large-cpu-image.tar.gz | docker image load
cp .env.example .env
# Edit MODEL_STORAGE_DIR to a disk on the server, and BIND_ADDRESS if needed.
docker compose up -d --no-build --pull never
docker compose exec local-llm ollama list
```

To build directly from this source directory instead of transferring the image:

```bash
cp .env.example .env
docker compose up -d --build
```

`MODEL_STORAGE_DIR` is mounted at `/models` inside the container; blobs are stored
in `/models/blobs/`, manifests in `/models/manifests/`. Existing downloads can be
copied from the workstation's `data/ollama/models/` to that directory. Copy only
complete registered models; a partial blob is not a usable model.

## Add the large model later, on the large server

The chosen 750 GB RAM preset is
[Qwen3-VL 235B-A22B Instruct BF16](https://ollama.com/library/qwen3-vl:235b-a22b-instruct-bf16).
The weights are about 471 GB; allow at least 500 GB of free disk and additional
system RAM for context and the OS. This download is deliberately a manual step:

```bash
docker compose exec local-llm ollama pull qwen3-vl:235b-a22b-instruct-bf16
```

For the workbench on another machine, set `BIND_ADDRESS` to this server's private
LAN address, restart the container, and enter `http://SERVER_PRIVATE_IP:11434` in
**Local model server URL**. The endpoint has no authentication; keep it on a trusted
private network. Choose the **750 GB** preset and **Check local server & models**,
then select the installed model for stages 03 and 06. The adapter forces CPU
execution for every request. Use **None** reasoning for this Instruct model.

With no weights, choose **No model · leave unvalidated** in both stage model menus.
Extraction, existing manufacturer mappings, structuring, duplicate comparison
and workbook integrity still run. Model checks are explicitly skipped and the
catalog remains unvalidated. A selected but missing model is never replaced by a
cloud model or silently treated as successful.

The 235B model has not been loaded or benchmarked on this workstation. Container
startup with no weights and CPU inference with an installed small model can be
tested locally; large-model throughput must be measured on the target server.

## HDD loading timeout

`OLLAMA_LOAD_TIMEOUT=2h` allows long load stalls on slow disks. Recreate the
container after changing this value; a simple restart does not apply changed
Compose environment settings:

```bash
sudo docker compose up -d --force-recreate local-llm
```

The workbench starts an empty CPU-only preload request alongside PDF extraction,
then waits for the weights before starting review's response timeout. Its separate
Model load timeout also defaults to two hours. Keep-loaded mode pins the current
model with `keep_alive=-1`; only one selected model loads at a time. Loading fully
into RAM requires enough free memory for weights, context and other services.
