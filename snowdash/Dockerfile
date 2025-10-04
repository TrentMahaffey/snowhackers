# snowdash/Dockerfile — Django web
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Optional: collect static (safe to keep or drop if you’re only serving templates)
# This uses the default settings module (snowdash.settings)
RUN python manage.py collectstatic --noinput || true

ENV PYTHONUNBUFFERED=1

# gunicorn is started by docker-compose command