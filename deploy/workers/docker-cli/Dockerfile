FROM docker:24.0.7-cli AS docker-client
FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl openssl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir websocket-client==1.8.0
COPY --from=docker-client /usr/local/bin/docker /usr/local/bin/docker
COPY biomerte /opt/biome/biomerte
COPY providers/mac-docker/mac_docker.py /opt/biome/providers/mac-docker/
COPY providers/pi-docker/docker_cli.py /opt/biome/providers/pi-docker/
COPY providers/mac-terminal/mac_provider.py providers/mac-terminal/persistent_transport.py /opt/biome/providers/mac-terminal/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BIOME_MAC_GATEWAY_STATE=/run/biome/mac-terminal \
    PYTHONPATH=/opt/biome:/opt/biome/providers/mac-terminal
WORKDIR /opt/biome
ENTRYPOINT ["python", "/opt/biome/providers/mac-docker/mac_docker.py"]
CMD ["serve"]
