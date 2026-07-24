# [DEPRECATED] Этот Dockerfile оставлен для справки.
# Текущий способ запуска — через Python venv. См. setup.sh и README.md.

FROM python:3.11-slim

WORKDIR /app
COPY server.py .

# Отключаем буферизацию stdout/stderr
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir mcp websockets
