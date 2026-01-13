FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/
RUN apt-get update && apt-get install -y build-essential libpq-dev && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get remove -y build-essential && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY . /app

# Collect static files so Django admin assets are available at runtime
RUN python manage.py collectstatic --noinput || true

ENV PORT=8001

CMD ["gunicorn", "cms_admin.wsgi:application", "--bind", "0.0.0.0:8001", "--workers", "2"]
