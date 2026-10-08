FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    HOME=/app \
    XDG_CACHE_HOME=/opt/cache
RUN mkdir -p /opt/cache /app/.copilot \
    && python -m copilot download-runtime

EXPOSE 8088

CMD ["python", "main.py"]
