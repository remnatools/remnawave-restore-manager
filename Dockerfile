FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    curl \
    dnsutils \
    iproute2 \
    && curl -fsSL https://get.docker.com | sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn python-multipart

COPY app/ /app/
COPY templates/ /app/templates/

RUN mkdir -p /app/data /app/ssl

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8443", \
     "--ssl-keyfile", "/app/ssl/privkey.key", \
     "--ssl-certfile", "/app/ssl/fullchain.pem"]
