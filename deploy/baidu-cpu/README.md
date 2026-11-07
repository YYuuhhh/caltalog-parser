# Experimental Unlimited-OCR on CPU

This is the original `baidu/Unlimited-OCR` model, pinned to Hugging Face revision
`8249d20b2822a7d8a4c22c8d8c9069b5ac36bda3`, not Unlimited-OCR-2 or PaddleOCR.
The upstream example is GPU based. The adapter verifies source hashes before
replacing 17 hardcoded CUDA transfers, three CUDA autocast contexts and fixed
BF16 image casts with CPU/device-aware operations. It adds the Transformers
generation mixin and uses eager attention, FP32 and greedy generation. The token
limit applies to newly generated tokens. No vLLM, FlashAttention or CUDA runtime
is installed.

**This CPU port is experimental. Real weight-backed inference and its accuracy
on the target server have not been validated yet.** Source patch checks, HTTP
contracts, cancellation, real CPU-only container startup and a tiny language-model
prefill/decode test have passed without OCR weights. It is an alternative to
MinerU in **Extract pages**, producing source-bound page artifacts and RAW tables
for subsequent reviews. It cannot approve semantic correctness, internet matches
or completeness.

## Prepare the optional runtime once

From the project directory on the server:

```bash
sudo docker build -f deploy/baidu-cpu/Dockerfile.dependencies -t catalogflow-baidu-deps:cpu-v1 .
sudo mkdir -p artifacts/baidu-cpu
sudo docker image save -o artifacts/baidu-cpu/dependencies.tar catalogflow-baidu-deps:cpu-v1
sudo sha256sum artifacts/baidu-cpu/dependencies.tar | sudo tee artifacts/baidu-cpu/dependencies.tar.sha256
sudo docker build --pull=false --network=none -f deploy/baidu-cpu/Dockerfile -t catalogflow-baidu-ocr:cpu-v1 .
sudo docker compose --profile baidu-ocr up -d --no-build --pull never baidu-ocr
```

The heavy Python/CPU libraries are in the separate dependency image. Its full
installed Python version graph is checked at startup. Keep the ordinary host
archive in `artifacts/baidu-cpu/` to restore it after deleting Docker images,
cache or volumes:

```bash
sudo sha256sum -c artifacts/baidu-cpu/dependencies.tar.sha256
sudo docker image load -i artifacts/baidu-cpu/dependencies.tar
sudo docker build --no-cache --pull=false --network=none -f deploy/baidu-cpu/Dockerfile -t catalogflow-baidu-ocr:cpu-v1 .
sudo docker compose --profile baidu-ocr up -d --no-build --pull never baidu-ocr
```

The optional image is separate from the default three-image runtime bundle.
For Docker installations without Buildx, prefix build commands with
`sudo env DOCKER_BUILDKIT=0 docker build ...`.

## Install and use

In **Extract pages**, expand **Optional Baidu CPU runtime**, click **Check CPU
OCR service**, then **Install Baidu weights**. Installation is explicit; starting the container does not download
weights or load a model. Follow the download with
`sudo docker compose logs -f baidu-ocr`. After installation, select
**Baidu Unlimited-OCR · experimental CPU** in **CPU extraction model**.
Each physical page is rendered at the selected DPI and sent to this CPU service.
Adjust the per-page output token budget for dense pages. Automatic quality
retries raise DPI up to 300 and retain an improved extraction only when it loses
no table cells. Pages without measurable text confidence go directly to model
review rather than repeating OCR without a way to compare quality. MinerU's native-text method, language group and formula switch
do not control this generative OCR model and are disabled for this selection.

Weights and patched sources persist in `data/baidu-ocr/` (override with
`BAIDU_OCR_DATA_DIR`). Jobs, raw transcripts, errors and token progress also live
there. Allow approximately 6 GB for weights and approximately 20–24 GiB free RAM
for FP32 inference; actual peak use must be measured on the target server.
The CPU model is loaded in a separate worker for each uncached page and released
when that page finishes. Existing Qwen and OCR allocations must fit together.
The adapter keeps one OCR job active and uses a bounded token limit. Stop or
timeout cancels the remote worker process; an incomplete or mismatched result
fails extraction. Completed pages are resumed only when their PDF/page hash,
model revision, DPI and extraction options match. Changing models or DPI creates
new immutable artifacts.

Each parsed page retains its PNG, raw OCR response, transcript, content list,
cache manifest and ZIP beneath `parsed/ui-runs/<run-id>/`. HTML and Markdown
tables feed the ordinary lossless RAW exporter. No table coordinates or model
confidence are fabricated. `logs/ui-runs/<run-id>/extraction-generation.json`
supplies extraction token progress to the UI. The later review stages inspect
these artifacts against the original PDF and frozen manufacturer web evidence.

Upstream: [model and code](https://huggingface.co/baidu/Unlimited-OCR),
[Baidu repository](https://github.com/baidu/Unlimited-OCR),
[GPU recipe supplied for this integration](https://recipes.vllm.ai/baidu/Unlimited-OCR?hardware=h100).
