FROM python:3.11-slim

# Python runtime env: unbuffered logs, no .pyc, src on the import path.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Real Chronos forecasting: CPU torch (small) + chronos-forecasting.
COPY requirements-ml.txt ./
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements-ml.txt

# Project metadata + source + config.
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
COPY config/ config/

# Install the package itself without re-resolving deps (already installed above).
RUN pip install --no-deps .

EXPOSE 8080

CMD ["python", "-m", "bacnet_lab"]
