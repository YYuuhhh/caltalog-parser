ARG BAIDU_RUNTIME_IMAGE=catalogflow-baidu-deps:cpu-v1
FROM ${BAIDU_RUNTIME_IMAGE}
ENV CUDA_VISIBLE_DEVICES="" HF_HUB_DISABLE_TELEMETRY=1 PYTHONUNBUFFERED=1 BAIDU_OCR_DATA=/models
COPY scripts/baidu_cpu_runtime.py scripts/baidu_cpu_service.py scripts/system_diagnostics.py /app/
RUN python -c "import torch, transformers; assert torch.version.cuda is None; assert torch.__version__ == '2.9.1+cpu'; assert transformers.__version__ == '4.52.1'"
WORKDIR /app
EXPOSE 8095
CMD ["python", "/app/baidu_cpu_service.py"]
