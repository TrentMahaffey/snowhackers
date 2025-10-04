# snowdash/Dockerfile
FROM python:3.11-slim

# Keep your existing apt setup, add libpq runtime for psycopg and build tools only if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata curl libpq5 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=snowdash.settings

WORKDIR /app

# Same flow: requirements first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app (Django project + dashboard app)
COPY . /app

# Collect static at build time (WhiteNoise will serve them)
RUN python manage.py collectstatic --noinput

# Expose matches compose mapping (optional)
EXPOSE 8000

# Swap your old "python -u main.py" for gunicorn
CMD ["gunicorn", "snowdash.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]