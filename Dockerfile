# Slim base instead of the full python:3.12 image - the full image carries
# ~300MB of build tools and docs this bot never uses; slim + the explicit
# apt packages below covers everything Pillow's native extensions need.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libavif-dev \
    libjpeg-dev \
    libpng-dev \
    libtiff-dev \
    libwebp-dev \
    libfreetype6-dev \
    libopenjp2-7-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
RUN python -m pip install --no-cache-dir --upgrade Pillow

COPY . /app

# Run as an unprivileged user rather than root - standard production
# hardening. The app only ever reads/writes its own working directory
# (Process/, session files, restart_msg.txt), so this needs no extra setup.
RUN useradd --create-home --uid 1000 botuser \
    && chown -R botuser:botuser /app
USER botuser

CMD ["bash", "start.sh"]
