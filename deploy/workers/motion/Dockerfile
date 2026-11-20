ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE}
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY deploy/workers/motion/requirements.txt /tmp/motion-requirements.txt
RUN pip install --no-cache-dir -r /tmp/motion-requirements.txt
COPY biomerte /app/biomerte
COPY providers/rover-motion /app/providers/rover-motion
COPY providers/mac-terminal/mac_provider.py providers/mac-terminal/persistent_transport.py /app/gateway/
COPY deploy/workers/motion /app/deploy/workers/motion
COPY tests/test_motion_capabilities.py tests/test_motion_model.py tests/test_motion_planning.py tests/test_motion_training.py /app/tests/
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIOME_GATEWAY_HOST_IO_CONFIG=/run/biome/host-io.json
ENTRYPOINT ["python", "/app/deploy/workers/motion/entrypoint.py"]
CMD ["--help"]
