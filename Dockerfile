# DHL Fleet Health Dashboard — production image (Dash WSGI via Gunicorn)
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt requirements-prod.txt ./
RUN pip install --upgrade pip && pip install -r requirements-prod.txt

COPY . .

# Dash callbacks + background prewarm share one process: use 1 worker.
# Threads handle concurrent HTTP clients.
ENV DHL_DASH_HOST=0.0.0.0
EXPOSE 8050

CMD ["gunicorn", "app:server", "-b", "0.0.0.0:8050", "--workers", "1", "--threads", "8", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-"]
