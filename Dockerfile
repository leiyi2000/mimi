FROM python:3.14-slim

# Install uv
RUN pip install uv

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml ./
COPY mimi/ ./mimi/
COPY plugins/ ./plugins-from-image/
COPY .env ./.env

# Install dependencies using uv
RUN uv sync

# Set the command to run the application
CMD ["uv", "run", "--env-file", ".env", "python3", "-m", "mimi.main"]