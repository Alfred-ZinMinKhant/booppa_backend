FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system packages needed for some wheels and runtime
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
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r /app/requirements.txt


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
