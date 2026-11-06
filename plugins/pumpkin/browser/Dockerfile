FROM h4ckf0r0day/obscura:latest AS obscura

FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 worker \
    && mkdir -p /run/pumpkin-browser /fonts \
    && chown -R worker:worker /run/pumpkin-browser /fonts
COPY --from=obscura /obscura /usr/local/bin/obscura
COPY protocol.py worker.py /app/
WORKDIR /app
USER worker
ENTRYPOINT ["python", "/app/worker.py"]
