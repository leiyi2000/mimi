FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends libseccomp2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --no-create-home --uid 10001 runner \
    && mkdir -p /run/pumpkin-browser /run/pumpkin-browser-sdk \
    && chown runner:runner /run/pumpkin-browser /run/pumpkin-browser-sdk
COPY __init__.py protocol.py sdk_protocol.py safe_sdk.py code_validator.py code_child.py child_entry.py code_runner.py /app/browser/
WORKDIR /app
USER runner
ENTRYPOINT ["python", "-m", "browser.code_runner"]
