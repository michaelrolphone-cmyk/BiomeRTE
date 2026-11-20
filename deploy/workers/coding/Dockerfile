# Actual coding MCP, filesystem tools and durable Biome job client.
ARG NODE_IMAGE=node:24-alpine@sha256:116b800c3754b440ee0aa3ed2f20f5bf3d99a554bfd9adeea09ea8017f6e85f5
FROM ${NODE_IMAGE}
ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/michaelrolphone-cmyk/BiomeRTE" \
      org.opencontainers.image.revision=${SOURCE_REVISION} \
      org.opencontainers.image.title="BiomeRTE coding tools"
RUN apk add --no-cache python3 ca-certificates openssl
WORKDIR /app/source
COPY deploy/workers/coding/source/coding-deps/package.json deploy/workers/coding/source/coding-deps/package-lock.json ./coding-deps/
RUN cd coding-deps && npm ci --omit=dev --ignore-scripts --no-audit --no-fund
COPY deploy/workers/coding/source/ /app/source/
COPY providers/mac-terminal/mac_provider.py providers/mac-terminal/persistent_transport.py /app/gateway/
COPY biomerte/__init__.py biomerte/host_io_client.py /app/biomerte/
COPY integrations/workspaces/manager.py integrations/workspaces/provider.py /app/workspaces/
COPY deploy/workers/coding/entry.py deploy/workers/coding/source-provenance.json /app/
COPY deploy/workers/coding/tests /app/tests
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/source:/app/gateway:/app/workspaces:/app \
    BIOME_GATEWAY_CODE=/app/gateway BIOME_WORKSPACES_CODE=/app/workspaces \
    BIOME_CONTAINER_WORKER=1 BIOME_GATEWAY_HOST_IO_CONFIG=/run/biome/host-io.json \
    BIOME_CODING_PYTHON=/usr/bin/python3 BIOME_CODING_LISTEN=0.0.0.0
RUN chmod -R a+rX /app
USER node
ENTRYPOINT ["python3","/app/entry.py"]
CMD ["check"]
