FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl openssl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir websocket-client==1.8.0

WORKDIR /app
COPY biomerte /app/biomerte
COPY providers/rover-motion/biome_provider.py /app/providers/rover-motion/
COPY providers/mac-terminal/mac_provider.py providers/mac-terminal/persistent_transport.py /app/gateway/
COPY deploy/workers/motion/entrypoint.py /app/entrypoint.py
COPY tests/test_motion_capabilities.py tests/test_motion_model.py tests/test_motion_planning.py tests/test_motion_training.py /app/tests/
ENV PYTHONPATH=/app:/app/gateway PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BIOME_MAC_GATEWAY_STATE=/run/biome/mac-terminal
ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["check"]
