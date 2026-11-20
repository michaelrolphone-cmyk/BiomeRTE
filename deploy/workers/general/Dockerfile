# Actual implementation images. Build from the repository root.
FROM docker:24.0.7-cli AS docker-client
FROM python:3.11-slim-bookworm AS base
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git openssh-client procps && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir websocket-client==1.8.0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIOME_CONTAINER_WORKER=1 \
    BIOME_GATEWAY_CODE=/opt/biome/providers/mac-terminal \
    BIOME_GATEWAY_HOST_IO_CONFIG=/run/biome/host-io.json \
    BIOME_WORKER_HEALTH=/run/biome-health/poll.json \
    PYTHONPATH=/opt/biome:/opt/biome/providers/mac-terminal
COPY biomerte /opt/biome/biomerte
COPY providers/mac-terminal/mac_provider.py providers/mac-terminal/persistent_transport.py /opt/biome/providers/mac-terminal/
COPY deploy/workers/general/health.py deploy/workers/general/entry.py /opt/
RUN chmod -R a+rX /opt/biome /opt/entry.py /opt/health.py
WORKDIR /opt/biome
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s CMD ["python","/opt/health.py"]
ENTRYPOINT ["python","/opt/entry.py"]

FROM base AS gitlab
COPY deploy/workers/runtime/gitlab/biomerte /opt/biome/biomerte
COPY providers/gitlab /opt/biome/providers/gitlab
RUN chmod -R a+rX /opt/biome
CMD ["gitlab"]

FROM base AS workspaces
COPY --from=docker-client /usr/local/bin/docker /usr/local/bin/docker
COPY deploy/workers/runtime/workspaces/biomerte /opt/biome/biomerte
COPY providers/gitlab /opt/biome/providers/gitlab
COPY integrations/workspaces /opt/biome/integrations/workspaces
ENV BIOME_GITLAB_CODE=/opt/biome/providers/gitlab
RUN chmod -R a+rX /opt/biome
CMD ["workspaces"]

FROM base AS docker-cli
COPY --from=docker-client /usr/local/bin/docker /usr/local/bin/docker
COPY deploy/workers/runtime/docker/biomerte /opt/biome/biomerte
COPY providers/mac-docker /opt/biome/providers/mac-docker
COPY providers/pi-docker/docker_cli.py /opt/biome/providers/pi-docker/docker_cli.py
COPY providers/pi-terminal /opt/biome/providers/pi-terminal
RUN chmod -R a+rX /opt/biome
CMD ["docker-cli"]

FROM base AS storage
COPY deploy/workers/runtime/terminal/biomerte /opt/biome/biomerte
COPY providers/mac-storage /opt/biome/providers/mac-storage
COPY providers/pi-storage /opt/biome/providers/pi-storage
ENV BIOME_STORAGE_PREMOUNTED=1
RUN chmod -R a+rX /opt/biome
CMD ["storage"]

FROM storage AS replica
# OpenSSH requires a passwd entry even when Docker runs a numeric UID.
ARG BIOME_NATIVE_UID=501
ARG BIOME_NATIVE_GID=20
RUN if ! getent group "$BIOME_NATIVE_GID" >/dev/null; then groupadd --gid "$BIOME_NATIVE_GID" biome-replica; fi \
    && useradd --no-create-home --uid "$BIOME_NATIVE_UID" --gid "$BIOME_NATIVE_GID" \
       --home-dir /nonexistent --shell /usr/sbin/nologin biome-replica
RUN chmod -R a+rX /opt/biome
CMD ["replica"]

FROM base AS private-base
RUN pip install --no-cache-dir cryptography==44.0.2
COPY deploy/workers/runtime/terminal/biomerte /opt/biome/biomerte
COPY providers/private-chat /opt/biome/providers/private-chat
COPY deploy/workers/private/health.py /opt/private-health.py
RUN chmod a+r /opt/private-health.py
ENV BIOME_MODEL_HOST_IO_CONFIG=/run/biome/host-io.json \
    BIOME_PRIVATE_CHAT_STATE=/state/chat BIOME_PRIVATE_IMAGE_STATE=/state/image \
    BIOME_PRIVATE_MODEL_HEALTH=/run/biome-health/model.json
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s CMD ["python","/opt/private-health.py"]

FROM private-base AS private-chat
RUN chmod -R a+rX /opt/biome
CMD ["private-chat"]

FROM private-base AS private-image
COPY providers/private-image /opt/biome/providers/private-image
RUN chmod -R a+rX /opt/biome
CMD ["private-image"]
