# Aware dashboard — self-contained static site + read-only Neon API.
# Build from this directory (it needs nothing from the rest of the repo):
#   docker build -t aware-dashboard dashboard/
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py index.html logical_levels.json ./

# Run as a non-root user — the deploy chart enforces runAsNonRoot + a read-only root
# filesystem. App files are world-readable and nothing is written at runtime.
RUN useradd --uid 1000 --create-home appuser
USER 1000

ENV PORT=8000
EXPOSE 8000
# DATABASE_URL (Neon, sslmode=require) is injected at runtime via env/secret.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
