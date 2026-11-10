FROM mcr.microsoft.com/playwright/python:v1.56.0-noble@sha256:a7f6cf3ae520c9d670ad956572c13747ed5abdbba5123a01526f873ed1662528

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp/browser-home

RUN pip install --no-cache-dir playwright==1.56.0 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 worker \
    && mkdir -p /run/pumpkin-browser /run/pumpkin-browser-sdk /tmp/browser-home \
    && chown -R worker:worker /run/pumpkin-browser /run/pumpkin-browser-sdk /tmp/browser-home

COPY __init__.py protocol.py sdk_protocol.py models.py navigation.py state_checks.py code_validator.py code_runner.py references.py dialogs.py engine.py worker.py /app/browser/
WORKDIR /app
USER worker
ENTRYPOINT ["python", "-m", "browser.worker"]
