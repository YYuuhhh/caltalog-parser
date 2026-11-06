ARG MINERU_RUNTIME_IMAGE=catalogflow-mineru-deps:3.4.5-cpu-v1
FROM ${MINERU_RUNTIME_IMAGE}
ARG MINERU_RUNTIME_LOCK_SHA=
ARG MINERU_MODEL_REVISION=mineru-3.4.5-pipeline
ENV CATALOGFLOW_DEPENDENCY_SHA256=${MINERU_RUNTIME_LOCK_SHA} \
    MINERU_DEVICE_MODE=cpu \
    MINERU_MODEL_SOURCE=local \
    MINERU_MODEL_REVISION=${MINERU_MODEL_REVISION} \
    MINERU_API_OUTPUT_ROOT=/parsed/_api_tasks \
    MINERU_API_TASK_RETENTION_SECONDS=3600 \
    MINERU_API_MAX_CONCURRENT_REQUESTS=1 \
    MINERU_TABLE_MERGE_ENABLE=0 \
    MINERU_UPSTREAM_URL=http://127.0.0.1:8001 \
    MINERU_CACHE_SCHEMA=v1 \
    PYTHONPATH=/app
COPY deploy/runtime/verify_runtime.py deploy/runtime/policy.json /opt/catalogflow/
COPY mineru/app.py mineru/entrypoint.sh /app/
RUN chmod 0755 /app/entrypoint.sh \
    && python /opt/catalogflow/verify_runtime.py fingerprint --role mineru --output /opt/mineru-runtime-fingerprint.txt
WORKDIR /app
EXPOSE 8000 7860
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["api"]
