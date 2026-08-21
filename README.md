# ESPHome Local MCP Server

[![Version](https://img.shields.io/badge/version-1.8.0-blue.svg)](CHANGELOG.md)
[![SemVer 2.0.0](https://img.shields.io/badge/SemVer-2.0.0-green.svg)](https://semver.org/)
[![Documentation](https://img.shields.io/badge/docs-API.md-orange.svg)](https://github.com/esphome/device-builder/blob/main/docs/API.md)

Локальный сервер протокола MCP (Model Context Protocol) для интеграции **ESPHome Device Builder** с AI-ассистентами (Cursor, VS Code Copilot, Claude Desktop, Gemini CLI, Antigravity).

Позволяет языковым моделям (LLM) автономно управлять всем жизненным циклом устройств ESPHome: валидировать конфигурации, компилировать и прошивать (OTA) прошивки, читать runtime-логи, расшифровывать дампы паники, производить поиск по всем конфигурациям, управлять платами, задачами сборки, архивом устройств и правами доступа — напрямую через WebSocket API ESPHome Device Builder.

## 🌟 Возможности

- **Интеграция через WebSockets:** Напрямую общается с локальным ESPHome Device Builder API (по умолчанию `ws://localhost:6052/ws`).
- **Гибкая конфигурация (.env):** Поддерживает чтение IP-адреса (`host`) и порта (`port`) сервера API из файла `.env` в формате TOML с автоматическим возвратом к `localhost:6052` при отсутствии файла или ошибках парсинга.
- **Умная обработка логов:** Очищает вывод ESPHome от ANSI-цветовых кодов для обеспечения чистого и читаемого ответа для LLM.
- **Двухшаговый трекинг задач:** Корректно подписывается на задачи компиляции и прошивки (`firmware/follow_job`) и дожидается их завершения (событие `result`).
- **Работа исключительно через API:** Вся информация о конфигурациях передается строго через WebSocket API ESPHome Device Builder без доступа к локальным файлам хоста.
- **Нормализация путей:** Поддерживает два формата параметра `configuration`:
  - `mcp-test.yaml` — имя конфигурации в ESPHome API
  - `config/mcp-test.yaml` — автоматически обрезает префикс `config/`
- **Гибкая OTA и Serial-прошивка:** Инструменты установки по умолчанию используют `port: "OTA"` для сетевой прошивки «по воздуху», а также поддерживают передачу явного IP-адреса/hostname или Serial-порта (`/dev/ttyUSB0`).
- **Проверка авторизации:** Сервер корректно определяет флаг `requires_auth` и предупредит агента, если ESPHome защищён паролем.
- **Семантическое версионирование (SemVer):** Встроенное отслеживание версий, журнал изменений [`CHANGELOG.md`](CHANGELOG.md) и инструмент `get_server_version`.

## 🛠 Доступные MCP Инструменты

Сервер предоставляет **23 инструмента** для AI-агентов, разбитых на 4 группы. Каждая команда поддерживает опциональное переопределение `host` и `port` (`api_port`).

---

### Группа 1: Базовые операции (Compile & Flash)

| # | Инструмент | API-команда | Описание |
|---|-----------|-------------|----------|
| 1 | `validate_yaml(configuration, host, port)` | `devices/validate` | Быстрая проверка синтаксиса и структуры YAML без компиляции C++ кода |
| 2 | `compile_firmware(configuration, force_local, host, port)` | `firmware/compile` | Полная компиляция прошивки без загрузки; поддерживает принудительную локальную сборку (`force_local`) |
| 3 | `flash_ota(configuration, port, bootloader, host, api_port)` | `firmware/upload` | Прошивка готового бинарника (по умолчанию `"OTA"`, явный IP или Serial); поддержка записи bootloader |
| 4 | `compile_and_flash(configuration, port, force_local, bootloader, host, api_port)` | `firmware/install` | Полный цикл сборки и прошивки (OTA / IP / Serial); поддержка `force_local` и `bootloader` |

---

### Группа 2: Мониторинг, Отладка и Сетевая Диагностика (P0/P2)

| # | Инструмент | API-команда | Описание |
|---|-----------|-------------|----------|
| 5 | `list_devices(host, port)` | `devices/list` | Список всех устройств с IP-адресами, статусами (online/offline), версиями прошивки и флагами незакомпилированных изменений |
| 6 | `stream_device_logs(configuration, port, duration_seconds, lines_count, host, api_port)` | `devices/logs` | Чтение runtime-логов работы устройства по OTA или Serial в реальном времени |
| 7 | `decode_crash_backtrace(configuration, lines, host, port)` | `devices/decode_backtrace` | Расшифровка C++ стектрейсов/дампов паники устройства с помощью `addr2line` и ELF-символов сборки |
| 8 | `search_yaml_configs(query, context_lines, case_sensitive, host, port)` | `yaml/search` | Полнотекстовый поиск подстроки по всем YAML-конфигурациям ESPHome с выводом контекстных строк |
| 9 | `troubleshoot_device(configuration, action, host, port)` | `devices/troubleshoot`, `devices/get_states` | Глубокая сетевая диагностика доступности узла (DNS resolve, mDNS/Zeroconf анонсы, ICMP Ping с замером RTT в мс) и получение матрицы онлайн/офлайн состояний |

---

### Группа 3: Управление Конфигурациями, Секретами, Метками, Платами и Сборками (P1)

| # | Инструмент | API-команды | Описание |
|---|-----------|-------------|----------|
| 10 | `manage_device_config(action, configuration, content, new_name, board_id, friendly_name, ssid, psk, config_only, overwrite, allow_wipe, host, port)` | `devices/get_config`, `devices/update_config`, `devices/create`, `devices/rename`, `devices/delete` | Управление YAML: CRUD, создание по шаблону платы (`board_id`), интеграция с `secrets.yaml` (Wi-Fi), офлайн (`config_only=True`) и онлайн (`config_only=False`) переименование |
| 11 | `migrate_device_config(configuration, content, apply, host, port)` | `editor/migrate_config`, `devices/get_config`, `devices/update_config` | Автоматическая миграция устаревшего YAML синтаксиса ESPHome (services ➔ actions, clk_mode ➔ clk и др.), получение patch diff и сохранение |
| 12 | `search_components(action, query, category, platform, component_id, limit, offset, host, port)` | `components/get_components`, `components/get_categories`, `components/get_pin_registry_modes` | Каталог компонентов ESPHome (>940 записей): поиск, зависимости (I2C/SPI/UART), ограничения шин, паспорт компонента, категории и режимы пинов |
| 13 | `manage_secrets(action, key, value, ssid, psk, host, port)` | `config/get_secrets`, `config/set_secret`, `config/set_wifi_credentials` | Безопасное управление секретами (`secrets.yaml`): просмотр доступных ключей (без раскрытия приватных значений), атомарная запись секретов и настройка Wi-Fi |
| 14 | `manage_labels(action, label_id, name, color, host, port)` | `labels/list`, `labels/create`, `labels/update`, `labels/delete` | Управление глобальным каталогом меток (тегов): создание меток с HEX-цветом (`#rrggbb`), редактирование, просмотр каталога и каскадное удаление |
| 15 | `batch_manage_devices(action, configurations, label_ids, updates, host, port)` | `devices/archive_bulk`, `devices/delete_bulk`, `devices/set_labels_bulk` | Пакетные операции над устройствами: массовая архивация, массовое безвозвратное удаление и массовое назначение меток на группу устройств |
| 16 | `get_host_info(action, host, port)` | `config/version`, `config/serial_ports` | Информация о хосте ESPHome: версии бэкенда/ESPHome Core и список обнаруженных физических USB-Serial адаптеров |
| 17 | `get_board_info(action, board_id, platform, variant, mcu, tag, query, limit, offset, host, port)` | `boards/get_boards`, `boards/get_board`, `boards/get_compatible_boards` | Каталог плат ESPHome: поиск по платформе, чипу (`variant`), MCU, тегам, пагинация (`offset`), полная распиновка и совместимость |
| 18 | `manage_build_jobs(action, configuration, job_id, status_filter, host, port)` | `firmware/get_jobs`, `firmware/get_job`, `firmware/cancel`, `firmware/clean`, `firmware/reset_build_env`, `firmware/clear`, `firmware/clear_queued_update` | Управление сборками: мониторинг задач, отмена, очистка кэша, сброс окружения, очистка истории задач (`clear`), сброс отложенных обновлений (`clear_queued`) |

---

### Группа 4: Пакетные Операции Сборки, Архивация, Безопасность и Версии (P2)

| # | Инструмент | API-команды | Описание |
|---|-----------|-------------|----------|
| 19 | `batch_compile_and_flash(configurations, action, port, force_local, bootloader, host, api_port)` | `firmware/compile_bulk`, `firmware/install_bulk` | Пакетная компиляция и/или OTA/Serial-прошивка группы устройств; поддерживает отложенные обновления (deferred install) для оффлайн-устройств, флаги `force_local` и `bootloader` |
| 20 | `archive_devices(action, configuration, host, port)` | `devices/archive`, `devices/unarchive`, `devices/list_archived`, `devices/delete_archived` | Мягкое удаление устройств в архив (обратимо), восстановление и просмотр архива |
| 21 | `manage_device_labels(configuration, label_ids, host, port)` | `devices/set_labels` | Установка/удаление меток (тегов) одного конкретного устройства |
| 22 | `authenticate_esphome(username, password, token, host, port)` | `auth/login` | Аутентификация на ESPHome-серверах, защищённых паролем (`requires_auth=true`) |
| 23 | `get_server_version()` | internal / SSOT | Получение информации о версии MCP-сервера (SemVer), протоколе и среде |

---

### Форматы параметра `configuration`

Инструменты, принимающие `configuration`, работают исключительно через ESPHome API:

| Формат | Пример | Поведение |
|--------|--------|-----------|
| Имя файла/конфигурации | `mcp-test.yaml` | Передаётся напрямую в ESPHome API |
| Относительный с префиксом | `config/mcp-test.yaml` | Префикс `config/` обрезается автоматически |

---

## 🚀 Установка и Настройка

Сервер запускается напрямую из Python-виртуального окружения (venv) — без Docker.

### 1. Клонирование репозитория и установка

```bash
git clone https://github.com/farmerkz/ESPHomeLocalMCP.git
cd ESPHomeLocalMCP
bash setup.sh
```

Скрипт создаст папку `.venv/` и установит необходимые зависимости (`mcp`, `websockets`).

### 2. Обновление сервера

Для получения последней версии сервера выполните следующие команды в директории проекта:

```bash
cd ESPHomeLocalMCP

# 1. Получить изменения из репозитория
git pull

# 2. Обновить зависимости (если изменился requirements.txt)
.venv/bin/pip install -r requirements.txt
```

> **Примечание:** Файл `.env` с вашими настройками при обновлении **не затрагивается** — он внесён в `.gitignore`.

После обновления **перезапустите** AI-ассистент или перезагрузите конфигурацию MCP-серверов, чтобы изменения вступили в силу.

### 3. Конфигурация (.env)

Сервер позволяет задавать параметры подключения к ESPHome в файле `.env` в формате TOML.

Создайте файл `.env` на основе шаблона `.env.example`:

```bash
cp .env.example .env
```

Содержимое `.env` для локального подключения:
```toml
# Настройки подключения к ESPHome Device Builder API (формат TOML)
host = "192.168.1.50"
port = 6052
```

Содержимое `.env` для подключения через **Traefik / HTTPS** (reverse proxy):
```toml
host = "esphome.example.com"
port = 443
# ssl = true   # можно не указывать — автоматически определится по порту 443
```

#### Параметр `ssl`

| Значение | Протокол | Описание |
|----------|----------|----------|
| `true`   | `wss://` | Принудительно WebSocket Secure (для Traefik/HTTPS) |
| `false`  | `ws://`  | Принудительно plain WebSocket |
| *(не задан)* | авто | `wss://` для портов 443/8443, иначе `ws://` |

> **Примечание:** Если файл `.env` отсутствует или содержит синтаксические ошибки, сервер автоматически перейдет на `localhost` и порт `6052`.

### 4. Подключение в Cursor / VS Code (Cline / Copilot) / Claude Desktop

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

### 5. Подключение в Gemini CLI

Для использования сервера с **Gemini CLI** зарегистрируйте его с помощью команды:

```bash
gemini mcp add esphome-local -- /ПОЛНЫЙ/ПУТЬ/К/ESPHomeLocalMCP/.venv/bin/python -u /ПОЛНЫЙ/ПУТЬ/К/ESPHomeLocalMCP/server.py
```

Либо добавьте конфигурацию в ваш файл `~/.gemini/config.json`:

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

### 6. Подключение в Antigravity (mcp_config.json)

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
- **Конфигурация:** Библиотека `tomllib` (стандартная в Python 3.11+) для считывания параметров IP и порта из `.env` файла.
- **Связь с ESPHome:** Библиотека `websockets`. Запросы отправляются в виде JSON-совместимых сообщений протокола ESPHome Device Builder API (обёртка параметров в `args`, ожидание потока событий `output` и завершающего события `result`).
- **Трёхрежимная работа с путями:** Для абсолютных путей используется временный конфиг через `devices/create`/`devices/delete`, реальное имя берётся из ответа API (ESPHome может slugify имя).

## 📝 Логирование и отладка

Сервер настроен на перенаправление всех логов в `stderr` (`sys.stderr`), так как `stdout` зарезервирован исключительно для передачи MCP-протокола.

Для просмотра логов в реальном времени (при ручном запуске):
```bash
.venv/bin/python -u server.py 2>mcp_debug.log
```

## 🗺 Дорожная карта развития (Roadmap)

Подробный план развития сервера, перечень задач по интеграции новых возможностей ESPHome Device Builder API и трекер статусов реализации доступны в файле [**`ROADMAP.md`**](ROADMAP.md).

## 🔖 Версионирование и релизы

Проект следует стандарту [Semantic Versioning 2.0.0](https://semver.org/lang/ru/) и ведет журнал изменений в [**`CHANGELOG.md`**](CHANGELOG.md).

Для повышения версии проекта используется встроенный скрипт:

```bash
# Повышение patch-версии (1.0.0 -> 1.0.1)
.venv/bin/python3 scripts/bump_version.py patch

# Повышение minor-версии (1.0.0 -> 1.1.0)
.venv/bin/python3 scripts/bump_version.py minor

# Просмотр изменений без применения (dry-run)
.venv/bin/python3 scripts/bump_version.py patch --dry-run
```

## 📦 Зависимости

Зависимости перечислены в `requirements.txt`:
- `mcp` — официальный Python SDK для Model Context Protocol
- `websockets` — асинхронная работа с WebSocket-соединениями
- `tomllib` / `tomli` — модуль для парсинга TOML конфигураций из `.env`


