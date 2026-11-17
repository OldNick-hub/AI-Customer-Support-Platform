# Python 3.12 Slim Base Image
FROM python:3.12-slim as base

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies (build-essential, OCR libraries, PDF libraries)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libpq-dev \
    tesseract-ocr \
    tesseract-ocr-spa \
    tesseract-ocr-fra \
    tesseract-ocr-deu \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application user and group (UID/GID 1000)
RUN groupadd -g 1000 appgroup && \
    useradd -u 1000 -g appgroup -s /bin/bash -m appuser

WORKDIR /app

# Pre-create writable runtime storage directories
RUN mkdir -p /app/storage/raw_documents \
             /app/storage/quarantine \
             /app/storage/indices \
             /app/storage/evidence \
             /app/storage/evidence_uploads \
             /tmp && \
    chown -R appuser:appgroup /app /tmp && \
    chmod -R 775 /app/storage /tmp

# Copy dependency definition and source
COPY pyproject.toml README.md /app/
COPY app /app/app
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini

# Install pip tools and dependencies
RUN pip install --upgrade pip && \
    pip install .

# Copy remaining application files and assign non-root ownership
COPY . /app/
RUN chown -R appuser:appgroup /app

# Switch to non-root user
USER appuser

# Expose FastAPI default port
EXPOSE 8000

# Default command: launch Uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
