FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system packages needed for Python wheels + runtime.
# NOTE: Chromium/Playwright is intentionally absent here — it lives only in the
# worker image (Dockerfile.worker). Keeping it out of this public-facing image
# removes the Chromium OS libs (libgbm/mesa) and the bundled Node driver from
# the surface scanned by the CI Trivy gate.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    pkg-config \
    libssl-dev \
    zlib1g-dev \
    libjpeg-dev \
    libfreetype6-dev \
    tzdata \
    curl \
    git \
    fonts-liberation \
    xmlsec1 \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt


# Copy application source
COPY . /app

# Entrypoint handles migrations and then starts the server
COPY ./entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create non-root user and take ownership
RUN useradd -m -u 1001 booppa || true
RUN chown -R booppa:booppa /app
USER booppa

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["/entrypoint.sh"]
