# ESPHome Local MCP Server

Локальный сервер протокола MCP (Model Context Protocol) для интеграции **ESPHome Device Builder** с AI-ассистентами (Cursor, VS Code Copilot, Claude Desktop, Antigravity). 

Позволяет языковым моделям (LLM) автономно валидировать, компилировать и прошивать (OTA) конфигурации ESPHome напрямую в локальной среде разработчика.

## 🌟 Возможности

- **Интеграция через WebSockets:** Напрямую общается с локальным ESPHome Device Builder API (по умолчанию `ws://localhost:6052/ws`).
- **Умная обработка логов:** Очищает вывод ESPHome от ANSI-цветовых кодов для обеспечения чистого и читаемого ответа для LLM.
- **Двухшаговый трекинг задач:** Корректно подписывается на задачи компиляции и прошивки (`firmware/follow_job`) и дожидается их завершения (событие `result`).
- **Нормализация путей:** Автоматически обрезает префикс `config/`, если модель передала полный относительный путь (например, `config/test.yaml` → `test.yaml`).
- **Гарантированная OTA-прошивка:** Инструменты установки явно используют параметр `port: "OTA"` для прошивки "по воздуху".
- **Проверка авторизации:** Сервер корректно определяет флаг `requires_auth` и предупредит агента, если ESPHome защищен паролем.

## 🛠 Доступные MCP Инструменты

Сервер предоставляет 4 инструмента (tools) для AI-агентов:

1. **`validate_yaml(configuration, host)`**
   Быстрая проверка синтаксиса и структуры YAML файла без компиляции C++ кода. Использует API `devices/validate`.
   
2. **`compile_firmware(configuration, host)`**
   Полный цикл генерации C++ кода и компиляции прошивки. Без прошивки устройства. Использует API `firmware/compile`.
   
3. **`flash_ota(configuration, host)`**
   Прошивка уже скомпилированного бинарника (OTA). Использует API `firmware/upload`.
   
4. **`compile_and_flash(configuration, host)`**
   Полный цикл: генерация C++, сборка и последующая заливка по воздуху (OTA). Использует API `firmware/install`.

*Параметр `host` по умолчанию равен `localhost`, что соответствует адресу локального ESPHome Device Builder.*

## 🚀 Установка и Запуск

Сервер запускается напрямую из Python-виртуального окружения (venv) — без Docker.

### 1. Установка

```bash
cd ESPHomeLocalMCP
bash setup.sh
```

Скрипт создаст папку `.venv/` и установит зависимости (`mcp`, `websockets`).

### 2. Подключение в Cursor / VS Code (Cline / Copilot) / Claude Desktop

Добавьте следующую конфигурацию в настройки MCP-серверов вашего редактора:

```json
{
  "mcpServers": {
    "esphome-local": {
      "command": "/ПОЛНЫЙ/ПУТЬ/К/ESPHomeLocalMCP/.venv/bin/python",
      "args": [
        "-u",
        "/ПОЛНЫЙ/ПУТЬ/К/ESPHomeLocalMCP/server.py"
      ]
    }
  }
}
```

> **Важно:** Укажите абсолютный путь до проекта. Флаг `-u` (unbuffered output для Python) обязателен для корректной работы stdio транспорта протокола MCP.

### 3. Подключение в Antigravity (mcp_config.json)

```json
{
  "mcpServers": {
    "esphome-local": {
      "command": "/ПОЛНЫЙ/ПУТЬ/К/ESPHomeLocalMCP/.venv/bin/python",
      "args": [
        "-u",
        "/ПОЛНЫЙ/ПУТЬ/К/ESPHomeLocalMCP/server.py"
      ]
    }
  }
}
```

## 🏗 Архитектура

- **Транспорт:** `stdio` (стандартный ввод-вывод) — стандарт для локальных MCP-серверов.
- **Фреймворк:** `mcp.server.fastmcp.FastMCP` (официальный Python SDK).
- **Окружение:** Python venv (`.venv/`), зависимости: `mcp`, `websockets`.
- **Связь с ESPHome:** Библиотека `websockets`. Запросы отправляются в виде JSON-RPC сообщений, совместимых с новым протоколом ESPHome Device Builder API (обертка параметров в `args`, ожидание потока `output` и события `result`).

## 📝 Логирование и отладка

Сервер настроен на перенаправление всех логов в `stderr` (`sys.stderr`), так как `stdout` зарезервирован исключительно для передачи MCP-протокола.

Для просмотра логов в реальном времени (при ручном запуске):
```bash
.venv/bin/python -u server.py 2>mcp_debug.log
```

## 📦 Зависимости

Зависимости перечислены в `requirements.txt`:
- `mcp` — официальный Python SDK для Model Context Protocol
- `websockets` — асинхронная работа с WebSocket-соединениями

> **Примечание:** Файлы `Dockerfile` и `docker-compose.yaml` сохранены в репозитории с пометкой `[DEPRECATED]` для исторической справки. Они больше не используются.
