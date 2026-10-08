FROM python:3.12-slim

# Ghostscript is optional; it adds an extra compression strategy.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ghostscript \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_TEMP_DIR=/tmp/audit_pdf_manager \
    WORKERS_PER_JOB=0
# Set APP_PASSWORD (and optionally APP_USERNAME) as a secret on the hosting platform.

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

RUN useradd --create-home --uid 1000 appuser && mkdir -p /tmp/audit_pdf_manager && chown appuser /tmp/audit_pdf_manager
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
# --proxy-headers: correct client/HTTPS information behind a hosting proxy (Hugging Face, nginx, ...)
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
