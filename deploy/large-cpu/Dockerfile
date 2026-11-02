# syntax=docker/dockerfile:1.7
ARG OLLAMA_VERSION=0.32.5
FROM ollama/ollama:${OLLAMA_VERSION} AS cpu-rootfs

# Keep Ollama's matching CPU backends and system libraries. Flatten the cleaned
# filesystem into the final image so CUDA/MLX/Vulkan layers are not shipped.
RUN rm -rf /usr/lib/ollama/cuda_v12 /usr/lib/ollama/cuda_v13 \
    /usr/lib/ollama/mlx_cuda_v13 /usr/lib/ollama/vulkan /usr/lib/ollama/include

FROM scratch
COPY --from=cpu-rootfs / /
LABEL org.opencontainers.image.title="CatalogFlow CPU inference server" \
    org.opencontainers.image.source="https://github.com/YYuuhhh/caltalog-parser" \
    org.opencontainers.image.description="CPU vision inference; no model weights or automatic downloads"
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LD_LIBRARY_PATH=/usr/lib/ollama \
    OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_MODELS=/models \
    OLLAMA_NO_CLOUD=1 \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_MAX_LOADED_MODELS=1 \
    OLLAMA_CONTEXT_LENGTH=32768 \
    OLLAMA_KEEP_ALIVE=60s \
    OLLAMA_VULKAN=0 \
    CUDA_VISIBLE_DEVICES=-1 \
    ROCR_VISIBLE_DEVICES=-1 \
    NVIDIA_VISIBLE_DEVICES=void
EXPOSE 11434
ENTRYPOINT ["/bin/ollama"]
CMD ["serve"]
