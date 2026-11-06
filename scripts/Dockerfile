ARG TOOLS_RUNTIME_IMAGE=catalogflow-tools-deps:py311-v1
FROM ${TOOLS_RUNTIME_IMAGE}
ARG TOOLS_RUNTIME_LOCK_SHA=
ENV CATALOGFLOW_DEPENDENCY_SHA256=${TOOLS_RUNTIME_LOCK_SHA} \
    PYTHONUNBUFFERED=1
COPY deploy/runtime/verify_runtime.py deploy/runtime/entrypoint.py deploy/runtime/policy.json /opt/catalogflow/
RUN python /opt/catalogflow/verify_runtime.py check --role tools
COPY scripts /scripts
COPY ui /ui
COPY schemas /schemas
COPY mappings /mappings
COPY tests /tests
COPY mineru /mineru-src
WORKDIR /work
ENTRYPOINT ["python", "/opt/catalogflow/entrypoint.py"]
