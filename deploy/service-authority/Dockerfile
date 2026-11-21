ARG PYTHON_IMAGE=python:3.11-alpine
FROM ${PYTHON_IMAGE} AS authority-runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AIOHTTP_NO_EXTENSIONS=1 MULTIDICT_NO_EXTENSIONS=1 \
    YARL_NO_EXTENSIONS=1 FROZENLIST_NO_EXTENSIONS=1 PROPCACHE_NO_EXTENSIONS=1
RUN apk add --no-cache openssl && pip install --no-cache-dir aiohttp==3.14.5
WORKDIR /app
COPY biomerte /app/biomerte
ENTRYPOINT ["python", "-m", "biomerte.service_daemon"]
CMD ["--role", "authority", "--config", "/config/service.json"]

# A prepared controller can remain fenced while its checkpoint is confirmed.
# Suppress startup probe failures without relaxing the normal post-ready checks.
FROM authority-runtime AS managed-controller
HEALTHCHECK --start-period=45s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8871/health',timeout=12).read()"]

FROM authority-runtime AS default
