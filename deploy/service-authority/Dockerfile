ARG PYTHON_IMAGE=python:3.11-alpine
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AIOHTTP_NO_EXTENSIONS=1 MULTIDICT_NO_EXTENSIONS=1 \
    YARL_NO_EXTENSIONS=1 FROZENLIST_NO_EXTENSIONS=1 PROPCACHE_NO_EXTENSIONS=1
RUN apk add --no-cache openssl && pip install --no-cache-dir aiohttp==3.14.5
WORKDIR /app
COPY biomerte /app/biomerte
ENTRYPOINT ["python", "-m", "biomerte.service_daemon"]
CMD ["--role", "authority", "--config", "/config/service.json"]
