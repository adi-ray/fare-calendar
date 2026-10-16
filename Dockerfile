# Fare Calendar API: serves a precomputed forecasts.json (no PyTorch in the image).
# Build forecasts where your logs live (scripts/build_artifact.py) and mount or upload them.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements-api.txt .
RUN pip install -r requirements-api.txt

COPY farecal ./farecal
COPY widget ./widget
COPY data/forecasts.SYNTHETIC.json ./data/forecasts.SYNTHETIC.json

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /var/data && chown app /var/data
USER app

# Uploads (POST /api/admin/artifact) and mounted forecasts live on /var/data.
# For a demo, run with -e FARECAL_ARTIFACT=/app/data/forecasts.SYNTHETIC.json
ENV FARECAL_ARTIFACT=/var/data/forecasts.json \
    PORT=8000
VOLUME ["/var/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8000'), timeout=4)"

CMD ["sh", "-c", "exec uvicorn farecal.api:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
