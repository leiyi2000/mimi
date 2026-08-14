FROM python:3.14-slim

# Install uv
RUN pip install uv

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml ./
COPY mimi/ ./mimi/
COPY plugins/ ./plugins/
COPY .env ./.env

# Install dependencies using uv
RUN uv sync

# Pre-sync each plugin's virtualenv so first run is fast
RUN for d in /app/plugins/*/; do [ -f "$d/pyproject.toml" ] && (cd "$d" && uv sync) || true; done

# Set the command to run the application
CMD ["uv", "run", "--env-file", ".env", "python3", "-m", "mimi.main"]