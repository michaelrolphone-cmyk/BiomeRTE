FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-numpy python3-serial \
       librtlsdr0 libusb-1.0-0 openssl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 pi \
    && useradd -u 1000 -g 1000 -m pi

WORKDIR /app
COPY biomerte /app/biomerte
COPY providers/pi-sdr /app/providers/pi-sdr
COPY deploy/workers/sdr/entrypoint.py /app/deploy/workers/sdr/entrypoint.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
USER 1000:1000
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/app/deploy/workers/sdr/entrypoint.py"]
CMD ["check"]
