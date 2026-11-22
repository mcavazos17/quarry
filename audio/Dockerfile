FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app/audio
COPY audio/*.py ./
ENV QUARRY_AUDIO_BIND=0.0.0.0 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1654:1654
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/health',timeout=3)"
CMD ["python","server.py"]
