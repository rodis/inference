# Aware dashboard — React/Vite SPA + read-mostly Neon API.
# Build from this directory (it needs nothing from the rest of the repo):
#   docker build -t aware-dashboard dashboard/

# --- stage 1: build the React bundle -------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
# npm ci when a lockfile is present (reproducible); fall back to install otherwise.
RUN npm ci || npm install
COPY web/ .
RUN npm run build          # → /web/dist

# --- stage 2: serve with FastAPI -----------------------------------------------
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py logical_levels.json ./
COPY --from=web /web/dist ./web/dist

# Run as a non-root user — the deploy chart enforces runAsNonRoot + a read-only root
# filesystem. App files are world-readable and nothing is written to disk at runtime.
RUN useradd --uid 1000 --create-home appuser
USER 1000

ENV PORT=8000
EXPOSE 8000
# DATABASE_URL (Neon, sslmode=require) is injected at runtime via env/secret.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
