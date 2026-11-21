ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE}
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY providers/robot-vision/requirements-streaming.txt /tmp/vision-streaming.txt
RUN pip install --no-cache-dir -r /tmp/vision-streaming.txt
COPY biomerte /app/biomerte
COPY providers/robot-vision /app/providers/robot-vision
COPY providers/mac-terminal/mac_provider.py providers/mac-terminal/persistent_transport.py /app/gateway/
COPY deploy/workers/vision /app/deploy/workers/vision
# Native maintenance source files may have an owner-only creation mask.
# The image contains code only; runtime secrets are separate read-only mounts.
RUN chmod -R a+rX /app
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIOME_GATEWAY_HOST_IO_CONFIG=/run/biome/host-io.json
ENTRYPOINT ["python", "/app/deploy/workers/vision/entrypoint.py"]
CMD ["--help"]
