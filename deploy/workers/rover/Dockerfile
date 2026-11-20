# Dedicated image for the actual paired Rover application and browser transport.
ARG PYTHON_IMAGE=python:3.11-alpine@sha256:7fbad8e4652eae0186429a6f26cf5b23e27e8c6fd7ac4d0cfd7a6988cdeea1e1
FROM ${PYTHON_IMAGE}
ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/michaelrolphone-cmyk/BiomeRTE" \
      org.opencontainers.image.revision=${SOURCE_REVISION} \
      org.opencontainers.image.title="BiomeRTE Rover application"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AIOHTTP_NO_EXTENSIONS=1 MULTIDICT_NO_EXTENSIONS=1 \
    YARL_NO_EXTENSIONS=1 FROZENLIST_NO_EXTENSIONS=1 PROPCACHE_NO_EXTENSIONS=1
RUN apk add --no-cache openssl && \
    pip install --no-cache-dir aiohttp==3.14.5 websockets==15.0.1 && \
    addgroup -g 1000 biome && adduser -D -H -u 1000 -G biome biome
WORKDIR /app
COPY deploy/portable-services/source/ /app/
COPY biomerte/host_io_client.py /app/biomerte/host_io_client.py
COPY deploy/portable-services/portable_common.py deploy/portable-services/rover.py /app/
COPY deploy/workers/rover/source-provenance.json /app/source-provenance.json
COPY deploy/workers/rover/tests /app/tests
RUN chmod -R a+rX /app
USER 1000:1000
ENTRYPOINT ["python","/app/rover.py"]
CMD ["--help"]
