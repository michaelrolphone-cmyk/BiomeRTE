ARG PYTHON_IMAGE=python:3.11-alpine@sha256:7fbad8e4652eae0186429a6f26cf5b23e27e8c6fd7ac4d0cfd7a6988cdeea1e1
FROM ${PYTHON_IMAGE}
ARG SOURCE_REVISION
ARG SOURCE_URL=https://github.com/michaelrolphone-cmyk/BiomeRTE
LABEL org.opencontainers.image.source=${SOURCE_URL} \
      org.opencontainers.image.revision=${SOURCE_REVISION} \
      org.opencontainers.image.title="BiomeRTE portable stream services"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AIOHTTP_NO_EXTENSIONS=1 MULTIDICT_NO_EXTENSIONS=1 \
    YARL_NO_EXTENSIONS=1 FROZENLIST_NO_EXTENSIONS=1 PROPCACHE_NO_EXTENSIONS=1
RUN apk add --no-cache openssl && \
    pip install --no-cache-dir aiohttp==3.14.5 websockets==15.0.1
WORKDIR /app
COPY deploy/portable-services/source/ /app/
COPY biomerte/host_io_client.py /app/biomerte/host_io_client.py
COPY deploy/portable-services/portable_common.py deploy/portable-services/router.py \
     deploy/portable-services/rover.py deploy/portable-services/probe.py /app/
COPY deploy/portable-services/source-provenance.json /app/source-provenance.json
RUN mkdir -p /state && python -m compileall -q /app
ENTRYPOINT ["python"]
CMD ["/app/router.py", "--config", "/config/router.json"]
