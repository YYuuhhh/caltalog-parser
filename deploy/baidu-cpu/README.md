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
prefill/decode test have passed without OCR weights. It supplies an independent OCR
transcript to the page/final reviewer; it cannot approve semantic correctness,
internet matches or completeness. It does not replace MinerU's RAW table export.

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

In **Review evidence**, click **Check CPU OCR service**, then **Install Baidu
weights**. Installation is explicit; starting the container does not download
weights or load a model. Follow the download with
`sudo docker compose logs -f baidu-ocr`. After installation, select
**Baidu Unlimited-OCR · experimental CPU** in **Independent OCR checking model**.

Weights and patched sources persist in `data/baidu-ocr/` (override with
`BAIDU_OCR_DATA_DIR`). Jobs, raw transcripts, errors and token progress also live
there. Allow approximately 6 GB for weights and approximately 20–24 GiB free RAM
for FP32 inference; actual peak use must be measured on the target server.
The CPU model is loaded in a separate worker for each uncached page and released
when that page finishes. Existing Qwen and OCR allocations must fit together.
The adapter keeps one OCR job active and uses a bounded token limit. Stop or
timeout cancels the remote worker process; an incomplete or mismatched result
blocks the selected check. Completed OCR evidence is cached by original image
SHA256, pinned revision and output limit, and never changes original RAW data.

The main pipeline saves `page-N-baidu.json` and `page-N-baidu-progress.json`
beside page evidence in `logs/ui-runs/<run-id>/`. The original image and the
transcript are included in fresh reviewer context. Model agreement is not proof
of correctness: the review must still inspect the PDF and official sources.

Upstream: [model and code](https://huggingface.co/baidu/Unlimited-OCR),
[Baidu repository](https://github.com/baidu/Unlimited-OCR),
[GPU recipe supplied for this integration](https://recipes.vllm.ai/baidu/Unlimited-OCR?hardware=h100).
