#!/usr/bin/env zsh
set -e
set -o pipefail

BASE_DIR="snowdash"

mkdir -p "$BASE_DIR"
cd "$BASE_DIR"

# requirements
cat > requirements.txt <<'EOF'
Django==5.0.7
psycopg[binary]==3.2.1
gunicorn==22.0.0
EOF

# Dockerfile
cat > Dockerfile <<'EOF'
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# default: gunicorn
CMD ["gunicorn", "snowdash.wsgi:application", "--bind", "0.0.0.0:8000"]
EOF

# manage.py
cat > manage.py <<'EOF'
#!/usr/bin/env python
import os, sys

def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'snowdash.settings')
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)

if __name__ == '__main__':
    main()
EOF
chmod +x manage.py

# project package
mkdir -p snowdash
cat > snowdash/__init__.py <<'EOF'
# empty
EOF

cat > snowdash/settings.py <<'EOF'
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "changeme")
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "snowdash.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "dashboard" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "snowdash.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "snow"),
        "USER": os.getenv("POSTGRES_USER", "snowuser"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "changeme"),
        "HOST": os.getenv("POSTGRES_HOST", "db"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
}

STATIC_URL = "static/"
EOF

cat > snowdash/urls.py <<'EOF'
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("dashboard.urls")),
]
EOF

cat > snowdash/wsgi.py <<'EOF'
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "snowdash.settings")
application = get_wsgi_application()
EOF

# app package
mkdir -p dashboard/templates/dashboard
cat > dashboard/__init__.py <<'EOF'
# empty
EOF

cat > dashboard/apps.py <<'EOF'
from django.apps import AppConfig

class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "dashboard"
EOF

cat > dashboard/urls.py <<'EOF'
from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("map/", views.map_view, name="map"),
]
EOF

cat > dashboard/views.py <<'EOF'
from django.shortcuts import render
from django.db import connection

def index(request):
    # quick sanity checks against your SnowAPI DB
    counts = {}
    with connection.cursor() as cur:
        cur.execute("select count(*) from public.snotel_station")
        counts["stations"] = cur.fetchone()[0]
        cur.execute("select count(*) from public.snotel_hourly_obs")
        counts["hourly_obs"] = cur.fetchone()[0]
        cur.execute("select count(*) from public.forecast_hourly")
        counts["forecast_rows"] = cur.fetchone()[0]
    return render(request, "dashboard/index.html", {"counts": counts})

def map_view(request):
    return render(request, "dashboard/map.html")
EOF

cat > dashboard/templates/dashboard/index.html <<'EOF'
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Snow Dashboard</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; padding: 24px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin: 16px 0; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 16px; }
    a.button { display: inline-block; padding: 8px 12px; border: 1px solid #333; border-radius: 6px; text-decoration: none; }
  </style>
</head>
<body>
  <h1>Snow Dashboard</h1>
  <p>Live data from your SnowAPI Postgres.</p>

  <div class="cards">
    <div class="card"><strong>Stations</strong><div style="font-size:28px;">{{ counts.stations }}</div></div>
    <div class="card"><strong>Hourly Obs</strong><div style="font-size:28px;">{{ counts.hourly_obs }}</div></div>
    <div class="card"><strong>Forecast Rows</strong><div style="font-size:28px;">{{ counts.forecast_rows }}</div></div>
  </div>

  <p><a class="button" href="{% url 'map' %}">View Map</a></p>
</body>
</html>
EOF

cat > dashboard/templates/dashboard/map.html <<'EOF'
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Snow Map</title>
</head>
<body>
  <h1>Snow Map</h1>
  <p>Interactive map with snow stations and forecasts will go here.</p>
  <p><a href="{% url 'index' %}">← Back</a></p>
</body>
</html>
EOF

echo "✅ Created Django scaffold at $(pwd)"