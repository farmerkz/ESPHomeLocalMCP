FROM python:3.11-slim

WORKDIR /app
COPY server.py .

# Отключаем буферизацию stdout/stderr
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir mcp websockets
