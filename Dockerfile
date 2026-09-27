# Universal worker image — Fly.io, Koyeb, Railway, any VPS (HF Docker Spaces are paid now → use hf_app.py).
# docker build -t epub-worker . && docker run -p 7860:7860 epub-worker
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860

# run as non-root
RUN useradd -m -u 1000 worker
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
USER worker

EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"7860\")}/')" || exit 1

# $PORT is honoured so the same image works on Railway / Koyeb / Fly (they inject PORT)
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1 --proxy-headers"]
