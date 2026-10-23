FROM python:3.14-slim

RUN pip install --no-cache-dir pillow requests
RUN useradd --create-home --uid 10001 runner
WORKDIR /app
COPY app.py /app/app.py
USER runner
CMD ["python3", "/app/app.py"]
