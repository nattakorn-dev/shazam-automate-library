# Dockerfile for Shazam Tagger Service
# Purpose: Identify songs using Shazam API and repair Thai encoding
#sudo docker build -t shazam-tagger-service:latest .
FROM python:3.11.15-slim-trixie

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    jq \
    curl \
    bash \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir \
    shazamio==0.8.1 \
    mutagen==1.47.0 \
    python-dotenv==1.2.2 \
    aiohttp==3.9.5 \
    requests==2.34.2

# Copy application files
COPY tagger_service.py .
#COPY .env .env 2>/dev/null || true

# Create necessary directories
RUN mkdir -p /music/watch /music/library /music/unmanage /logs

# Health check endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

EXPOSE 5000

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    SHAZAM_DELAY=1.5 \
    TZ=Asia/Bangkok

# Run tagger service
CMD ["python", "-u", "tagger_service.py"]
