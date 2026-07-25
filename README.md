# ESPHome Local MCP Server

Локальный сервер протокола MCP (Model Context Protocol) для интеграции **ESPHome Device Builder** с AI-ассистентами (Cursor, VS Code Copilot, Claude Desktop, Antigravity).

Позволяет языковым моделям (LLM) автономно управлять всем жизненным циклом устройств ESPHome: валидировать конфигурации, компилировать и прошивать (OTA) прошивки, читать runtime-логи, расшифровывать дампы паники, производить поиск по всем конфигурациям, управлять платами, задачами сборки, архивом устройств и правами доступа — напрямую через WebSocket API ESPHome Device Builder.

## 🌟 Возможности

- **Интеграция через WebSockets:** Напрямую общается с локальным ESPHome Device Builder API (по умолчанию `ws://localhost:6052/ws`).
- **Умная обработка логов:** Очищает вывод ESPHome от ANSI-цветовых кодов для обеспечения чистого и читаемого ответа для LLM.
- **Двухшаговый трекинг задач:** Корректно подписывается на задачи компиляции и прошивки (`firmware/follow_job`) и дожидается их завершения (событие `result`).
- **Нормализация путей:** Поддерживает три формата параметра `configuration`:
  - `mcp-test.yaml` — имя файла в конфиг-директории ESPHome
  - `config/mcp-test.yaml` — автоматически обрезает префикс `config/`
  - `/абсолютный/путь/к/mcp-test.yaml` — читает файл с диска, временно создаёт конфиг в ESPHome через `devices/create` и гарантированно удаляет его после выполнения команды (`try/finally`)
- **Гарантированная OTA-прошивка:** Инструменты установки явно используют параметр `port: "OTA"` для прошивки «по воздуху».
- **Проверка авторизации:** Сервер корректно определяет флаг `requires_auth` и предупредит агента, если ESPHome защищён паролем.

## 🛠 Доступные MCP Инструменты

Сервер предоставляет **15 инструментов** для AI-агентов, разбитых на 4 группы.

---

### Группа 1: Базовые операции (Compile & Flash)

| # | Инструмент | API-команда | Описание |
|---|-----------|-------------|----------|
| 1 | `validate_yaml(configuration, host)` | `devices/validate` | Быстрая проверка синтаксиса и структуры YAML без компиляции C++ кода |
| 2 | `compile_firmware(configuration, host)` | `firmware/compile` | Полная компиляция прошивки без прошивки устройства |
| 3 | `flash_ota(configuration, host)` | `firmware/upload` | Прошивка уже скомпилированного бинарника по воздуху (OTA) |
| 4 | `compile_and_flash(configuration, host)` | `firmware/install` | Полный цикл: компиляция и OTA-прошивка одной командой |

---

### Группа 2: Мониторинг и Отладка (P0)

| # | Инструмент | API-команда | Описание |
|---|-----------|-------------|----------|
| 5 | `list_devices(host)` | `devices/list` | Список всех устройств с IP-адресами, статусами (online/offline), версиями прошивки и флагами незакомпилированных изменений |
| 6 | `stream_device_logs(configuration, port, duration_seconds, lines_count, host)` | `devices/logs` | Чтение runtime-логов работы устройства по OTA или Serial в реальном времени |
| 7 | `decode_crash_backtrace(configuration, lines, host)` | `devices/decode_backtrace` | Расшифровка C++ стектрейсов/дампов паники устройства с помощью `addr2line` и ELF-символов сборки |
| 8 | `search_yaml_configs(query, context_lines, case_sensitive, host)` | `yaml/search` | Полнотекстовый поиск подстроки по всем YAML-конфигурациям ESPHome с выводом контекстных строк |

---

### Группа 3: Управление Конфигурациями, Платами и Сборками (P1)

| # | Инструмент | API-команды | Описание |
|---|-----------|-------------|----------|
| 9 | `manage_device_config(action, configuration, content, new_name, host)` | `devices/get_config`, `devices/update_config`, `devices/create`, `devices/rename`, `devices/delete` | CRUD для YAML-конфигураций: чтение, запись, создание, переименование и удаление через API |
| 10 | `get_board_info(action, board_id, platform, query, limit, host)` | `boards/get_boards`, `boards/get_board`, `boards/get_compatible_boards` | Каталог плат ESPHome: поиск, полная информация (распиновка, features, docs), список взаимозаменяемых плат |
| 11 | `manage_build_jobs(action, configuration, job_id, status_filter, host)` | `firmware/get_jobs`, `firmware/get_job`, `firmware/cancel`, `firmware/clean`, `firmware/reset_build_env` | Управление очередью сборки: просмотр задач, отмена, очистка кэша сборки устройства, глобальный сброс `.esphome/` |

---

### Группа 4: Пакетные Операции, Архивация и Безопасность (P2)

| # | Инструмент | API-команды | Описание |
|---|-----------|-------------|----------|
| 12 | `batch_compile_and_flash(configurations, action, port, host)` | `firmware/compile_bulk`, `firmware/install_bulk` | Пакетная компиляция и/или OTA-прошивка группы устройств; поддерживает отложенные обновления (deferred install) для оффлайн-устройств |
| 13 | `archive_devices(action, configuration, host)` | `devices/archive`, `devices/unarchive`, `devices/list_archived`, `devices/delete_archived` | Мягкое удаление устройств в архив (обратимо), восстановление и просмотр архива |
| 14 | `manage_device_labels(configuration, label_ids, host)` | `devices/set_labels` | Установка/удаление меток (тегов) устройств для организации парка |
| 15 | `authenticate_esphome(username, password, token, host)` | `auth/login` | Аутентификация на ESPHome-серверах, защищённых паролем (`requires_auth=true`) |

---

### Форматы параметра `configuration`

Инструменты из групп 1–3, принимающие `configuration`, поддерживают три формата:

| Формат | Пример | Поведение |
|--------|--------|-----------|
| Имя файла | `mcp-test.yaml` | Передаётся напрямую в ESPHome API |
| Относительный с префиксом | `config/mcp-test.yaml` | Префикс `config/` обрезается автоматически |
| **Абсолютный путь** | `/Users/user/project/mcp-test.yaml` | Файл читается с диска, временно создаётся в ESPHome и гарантированно удаляется после выполнения (`try/finally`) |

---

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

---

## 🏗 Архитектура

- **Транспорт:** `stdio` (стандартный ввод-вывод) — стандарт для локальных MCP-серверов.
- **Фреймворк:** `mcp.server.fastmcp.FastMCP` (официальный Python SDK).
- **Окружение:** Python venv (`.venv/`), зависимости: `mcp`, `websockets`.
- **Связь с ESPHome:** Библиотека `websockets`. Запросы отправляются в виде JSON-совместимых сообщений протокола ESPHome Device Builder API (обёртка параметров в `args`, ожидание потока событий `output` и завершающего события `result`).
- **Трёхрежимная работа с путями:** Для абсолютных путей используется временный конфиг через `devices/create`/`devices/delete`, реальное имя берётся из ответа API (ESPHome может slugify имя).

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
