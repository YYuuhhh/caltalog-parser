# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.11
ARG PYTORCH_GPU_IMAGE=pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime@sha256:7b324d212a4450795b49edba9949b7cdc72429148a64e974334bfe5774d51385
ARG MINERU_TARGET=cpu
FROM python:${PYTHON_VERSION}-slim-bookworm AS base

ARG MINERU_VERSION=3.4.5

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    MINERU_VERSION=${MINERU_VERSION}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        fontconfig \
        fonts-noto-cjk \
        fonts-noto-core \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        poppler-utils \
        tini \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

FROM base AS cpu-deps

RUN python -m pip install \
        --index-url https://download.pytorch.org/whl/cpu \
        'torch==2.9.1+cpu' \
        'torchvision==0.24.1+cpu' \
    && python -m pip install \
        "mineru[pipeline,gradio]==${MINERU_VERSION}" \
        'lxml>=5,<7' \
        'cryptography>=43,<47' \
        'openpyxl>=3.1,<4' \
        'XlsxWriter>=3.2,<4' \
        'pytest>=8,<9'

FROM ${PYTORCH_GPU_IMAGE} AS gpu-deps

ARG MINERU_VERSION=3.4.5

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    MINERU_VERSION=${MINERU_VERSION}

# The optional target starts from PyTorch's pinned CUDA runtime image. Keeping
# it in a separate build stage means the default CPU build never pulls CUDA.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        fontconfig \
        fonts-noto-cjk \
        fonts-noto-core \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        poppler-utils \
        tini \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install \
        "mineru[pipeline,gradio,vlm]==${MINERU_VERSION}" \
        'lxml>=5,<7' \
        'cryptography>=43,<47' \
        'openpyxl>=3.1,<4' \
        'XlsxWriter>=3.2,<4' \
        'pytest>=8,<9'

FROM cpu-deps AS cpu

ENV MINERU_DEVICE_MODE=cpu

FROM gpu-deps AS gpu

ENV MINERU_DEVICE_MODE=cuda

FROM ${MINERU_TARGET} AS runtime

ARG MINERU_MODEL_SOURCE=huggingface
ARG MINERU_MODEL_REVISION=mineru-3.4.5-pipeline

# Upstream's own Dockerfile downloads weights into the image. Keep the default
# CPU target pipeline-only and the optional GPU target VLM-only; neither image
# needs to download the other backend's multi-gigabyte model set.
ARG MINERU_TARGET
RUN if [ "${MINERU_TARGET}" = "cpu" ]; then \
      mineru-models-download -s "${MINERU_MODEL_SOURCE}" -m pipeline; \
    fi

# The optional GPU target also carries MinerU's fully local Transformers VLM.
# CPU remains pipeline-only so the default image and runtime stay smaller.
RUN if [ "${MINERU_TARGET}" = "gpu" ]; then \
      mineru-models-download -s "${MINERU_MODEL_SOURCE}" -m vlm; \
    fi

# MinerU's OCR operators import six without declaring it as a dependency.
# Check the lazy OCR imports here; /health alone does not exercise them.
RUN python -m pip install 'six==1.17.0' \
    && python -c "from mineru.model.utils.pytorchocr.data.imaug import operators"

WORKDIR /app

COPY app.py /app/app.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod 0755 /app/entrypoint.sh

# The cache key includes this build artifact. It captures the installed Python
# dependency graph, wrapper source, and actual downloaded model filenames,
# sizes and content hashes, so code or weight changes cannot reuse old pages.
RUN { \
      python -m pip freeze; \
      sha256sum /app/app.py /app/entrypoint.sh; \
      if [ -f /root/mineru.json ]; then sha256sum /root/mineru.json; fi; \
      for model_root in /root/.cache/huggingface/hub /root/.cache/modelscope/hub; do \
        if [ -d "${model_root}" ]; then \
          find "${model_root}" -type f \
            ! -path '*/.locks/*' ! -path '*/logs/*' -print0 \
            | sort -z | xargs -0 -r sha256sum; \
        fi; \
      done; \
    } > /opt/mineru-runtime-fingerprint.txt

ENV MINERU_MODEL_SOURCE=local \
    MINERU_MODEL_REVISION=${MINERU_MODEL_REVISION} \
    MINERU_API_OUTPUT_ROOT=/parsed/_api_tasks \
    MINERU_API_TASK_RETENTION_SECONDS=3600 \
    MINERU_API_MAX_CONCURRENT_REQUESTS=1 \
    MINERU_TABLE_MERGE_ENABLE=0 \
    MINERU_UPSTREAM_URL=http://127.0.0.1:8001 \
    MINERU_CACHE_SCHEMA=v1 \
    PYTHONPATH=/app

EXPOSE 8000 7860

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["api"]
