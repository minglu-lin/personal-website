FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ /app/server/
COPY config/ /app/config/

EXPOSE 18890

ENV PORT=18890
ENV HOST=0.0.0.0

CMD ["python", "server/scihub_batch_server.py"]
