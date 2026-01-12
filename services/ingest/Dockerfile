FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
ENV PYTHONPATH=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        python3-dev \
        build-essential \
        libpq-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install -U pip setuptools wheel \
    && pip install --no-cache-dir --verbose -r requirements.txt

COPY shared ./shared
COPY services/ingest ./services/ingest

CMD ["python", "-m", "services.ingest.main"]
