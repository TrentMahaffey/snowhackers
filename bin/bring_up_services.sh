docker compose up -d db
# wait for "healthy"
docker compose ps
docker compose up -d snowdash
docker compose logs -f snowdash
# then hit http://localhost:8000