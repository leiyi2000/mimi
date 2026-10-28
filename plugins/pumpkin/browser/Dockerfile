FROM h4ckf0r0day/obscura:latest AS obscura

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 worker \
    && mkdir -p /run/pumpkin-browser /fonts \
    && chown -R worker:worker /run/pumpkin-browser /fonts
COPY --from=obscura /obscura /usr/local/bin/obscura
COPY protocol.py worker.py /app/
WORKDIR /app
USER worker
ENTRYPOINT ["python", "/app/worker.py"]
