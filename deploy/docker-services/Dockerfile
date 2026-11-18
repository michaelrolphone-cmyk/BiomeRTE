ARG PYTHON_IMAGE=python:3.11-alpine
FROM ${PYTHON_IMAGE}
WORKDIR /app
COPY biomerte/docker_services.py biomerte/service_adapters.py /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/docker_services.py"]
CMD ["--config", "/config/runner.json"]
