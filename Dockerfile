# One image for production: the API plus the built dashboard.
#   docker build -t gh-ecosystem-analyzer .
#   docker run -p 8000:8000 -e DATABASE_URL=postgresql://... gh-ecosystem-analyzer

# --- Stage 1: build the dashboard ---------------------------------------------------------
FROM node:24-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: the API, serving the dashboard from /app/static -----------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     PIP_NO_CACHE_DIR=1     STATIC_DIR=/app/static     PORT=8000

# Same layout as the repo (backend/ next to db/), because the migration runner finds
# db/migrations relative to the source tree.
WORKDIR /app/backend
COPY backend/pyproject.toml ./
COPY backend/app ./app
COPY backend/ingest ./ingest
COPY backend/scripts ./scripts
RUN pip install -e .
COPY db /app/db
COPY --from=web /web/dist /app/static

RUN useradd --system --no-create-home app
USER app

EXPOSE 8000
# Hosts such as Render and Fly set PORT; default to 8000 elsewhere.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
