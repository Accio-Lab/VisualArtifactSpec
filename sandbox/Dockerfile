FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash coreutils nodejs npm libreoffice-impress libreoffice-writer libreoffice-calc \
    poppler-utils ffmpeg fonts-dejavu-core fonts-noto-cjk fonts-liberation \
    ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    playwright==1.56.0 Pillow==11.3.0 python-pptx==1.0.2 \
    openpyxl==3.1.5 pypdf==6.1.1 \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/browsers \
    && rm -rf /var/lib/apt/lists/*
COPY sandbox/observe.py /opt/vas/observe.py
WORKDIR /work
CMD ["sleep", "infinity"]
