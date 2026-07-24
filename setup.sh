#!/bin/bash
# Скрипт установки окружения для ESPHome Local MCP Server

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Создаём виртуальное окружение Python (.venv)..."
python3 -m venv .venv

echo "==> Устанавливаем зависимости из requirements.txt..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt

echo ""
echo "✅ Готово! Виртуальное окружение создано в .venv/"
echo ""
echo "Для подключения в Cursor / VS Code / Claude Desktop используйте конфиг:"
echo ""
echo "  {"
echo "    \"mcpServers\": {"
echo "      \"esphome-local\": {"
echo "        \"command\": \"$SCRIPT_DIR/.venv/bin/python\","
echo "        \"args\": [\"-u\", \"$SCRIPT_DIR/server.py\"]"
echo "      }"
echo "    }"
echo "  }"
