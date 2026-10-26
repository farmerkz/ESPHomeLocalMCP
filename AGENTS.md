# AGENTS.md — Инструкции для AI-агентов (ESPHome Local MCP Server)

Единое руководство по архитектуре проекта **ESPHome Local MCP Server**, особенностям протокола ESPHome Device Builder WebSocket API и критическим правилам разработки.

---

## 1. Обзор проекта и связанная документация

**ESPHome Local MCP Server** — локальный MCP-сервер (Model Context Protocol) на Python (`FastMCP`, транспорт `stdio`), предоставляющий AI-агентам 29 специализированных инструментов, 4 контекстных ресурса и 3 интерактивных промпта для управления полным жизненным циклом устройств ESPHome через WebSocket API.

- **Общее описание и установка**: [README.md](README.md)
- **Полный справочник всех 29 инструментов, ресурсов и сценариев**: [docs/ESPHOME_AGENT_GUIDE.md](docs/ESPHOME_AGENT_GUIDE.md)
- **Руководство по подключению в IDE (Antigravity, Cursor, Copilot и др.)**: [docs/USER_INTEGRATION_GUIDE.md](docs/USER_INTEGRATION_GUIDE.md)
- **Журнал изменений и версия**: [CHANGELOG.md](CHANGELOG.md), [\_\_version\_\_.py](__version__.py)
- **Официальная спецификация ESPHome Device Builder API**: `https://github.com/esphome/device-builder/blob/main/docs/API.md`
- **Целевой репозиторий конфигураций ESPHome**: `/Users/andreyzolotnitskiy/Documents/github/esphome`

---

## 2. Архитектура сервера и технологический стек

- **Основной модуль**: [server.py](server.py)
- **Транспорт MCP**: `stdio` (стандартный ввод-вывод JSON-RPC).
- **Фреймворк и стек**: Python 3.11+ (виртуальное окружение `.venv/`), `mcp.server.fastmcp.FastMCP`, `websockets`, `tomllib` / `tomli`.
- **Подключение к ESPHome Daemon**: WebSocket `ws://<host>:<port>/ws` или `wss://<host>:<port>/ws` (при `ssl = true` или портах 443/8443).
- **Конфигурация окружения**: считывается из `.env` в формате TOML (`host`, `port`, `ssl`) с автоматическим fallback на `localhost:6052`.
- **Логирование**: строго в `sys.stderr` (`stdout` зарезервирован под JSON-RPC протокол MCP).

---

## 3. Архитектурные категории MCP-компонентов

Полные сигнатуры и примеры вызовов приведены в [docs/ESPHOME_AGENT_GUIDE.md](docs/ESPHOME_AGENT_GUIDE.md):

1. **Сборка, прошивка и Canary Rollback (5 инструментов)**:
   `validate_yaml`, `compile_firmware`, `flash_ota`, `compile_and_flash`, `flash_canary_ota`.
2. **Мониторинг, сетевая диагностика и аппаратный аудит GPIO (6 инструментов)**:
   `list_devices`, `stream_device_logs`, `decode_crash_backtrace`, `search_yaml_configs`, `troubleshoot_device`, `validate_hardware_pinout`.
3. **Управление YAML, секретами, платами, компонентами и AST автоматизаций (10 инструментов)**:
   `manage_device_config`, `migrate_device_config`, `manage_automations`, `search_components`, `manage_secrets`, `manage_labels`, `batch_manage_devices`, `get_host_info`, `get_board_info`, `manage_build_jobs`.
4. **Пакетные операции, артефакты, архив, Git-история и кластерная сборка (8 инструментов)**:
   `batch_compile_and_flash`, `get_firmware_binaries`, `archive_devices`, `manage_device_labels`, `manage_version_history`, `manage_remote_build`, `authenticate_esphome`, `get_server_version`.
5. **Контекстные ресурсы (4 ресурса)**:
   `esphome://devices`, `esphome://configs/{configuration}`, `esphome://components/{component_id}`, `esphome://boards/{board_id}`.
6. **Интерактивные промпты (3 промпта)**:
   `new_device_wizard`, `troubleshoot_offline_device`, `safe_firmware_upgrade`.

---

## 4. Критические правила разработки и протокола ESPHome WebSocket API

При любой модификации [server.py](server.py) строго соблюдайте следующие правила:

### 4.1. Нормализация путей (`configuration`)
- Все инструменты работают через WebSocket API без прямого чтения локальной ФС.
- Префикс `config/` в параметре `configuration` обрезается автоматически (`config/test.yaml` ➔ `test.yaml`).
- В пакетных инструментах (`batch_compile_and_flash`, `batch_manage_devices`) списки `configurations` и `label_ids` передаются строго как массивы строк (`list[str]`).

### 4.2. Имена событий (Events) в потоковых командах
- При вызове потоковых команд (`firmware/follow_job`, `devices/validate`) сервер ESPHome возвращает сообщения типов **`"output"`** (строки лога) и **`"result"`** (финальный статус).
- События `job_output`, `job_completed`, `job_failed` используются **только** в глобальной шине `subscribe_events` — не путайте их!

### 4.3. Авторизация и `ServerInfoMessage`
- Сразу после открытия WebSocket-соединения сервер отправляет `ServerInfoMessage`.
- Обязательно считывайте его (`await asyncio.wait_for(ws.recv(), timeout=2.0)`) и проверяйте `requires_auth`.

### 4.4. Запрет вывода в `stdout`
- Любые вызовы `print()` в коде сервера **категорически запрещены**, так как они разрушают JSON-RPC поток MCP в `stdout`. Используйте только `logger` с выводом в `sys.stderr`.

### 4.5. Особенности двухшаговых и потоковых команд API
- `firmware/compile`, `firmware/upload`, `firmware/install`, `firmware/clean`, `firmware/reset_build_env` возвращают `job_id`, после чего требуют подписки через `firmware/follow_job`.
- `devices/logs` — бесконечный поток; всегда оборачивайте чтение в `asyncio.wait_for(..., timeout)`.
- `devices/create` может преобразовать имя файла (slugify) — всегда используйте реальное имя из `result.configuration`.

---

## 5. Тестирование и контроль качества

- **Тестовый раннер**: [test/test_mcp_server.py](test/test_mcp_server.py)
- **Манифест разрешённых тестовых устройств**: [test/test_devices.json](test/test_devices.json) (`test.yaml`, `liligo-t-internet.yaml`, `r5-gate-01.yaml` с флагами `allow_compile`, `allow_ota_flash`, `allow_state_mutation`). Запрещено использовать в тестах любые рабочие устройства вне этого манифеста!
- **Команда запуска тестов**:
  ```bash
  .venv/bin/python3 test/test_mcp_server.py
  ```

---

## 6. Версионирование (SemVer), документация и Git Workflow

1. **Единый источник версии**: файл [\_\_version\_\_.py](__version__.py) (`__version__ = "X.Y.Z"`).
2. **Скрипт повышения версии**:
   ```bash
   .venv/bin/python3 scripts/bump_version.py patch|minor|major
   ```
3. **Синхронизация документации**: при добавлении или изменении инструментов/параметров в [server.py](server.py) обязательно синхронно обновлять [README.md](README.md), [docs/ESPHOME_AGENT_GUIDE.md](docs/ESPHOME_AGENT_GUIDE.md), [CHANGELOG.md](CHANGELOG.md) и настоящий [AGENTS.md](AGENTS.md).
4. **Контроль версий и коммиты**: все коммиты и расчёт версий выполняются по глобальному навыку `git-release-workflow` через MCP-сервер `release-manager` (`analyze_staged_changes_for_commit` ➔ `validate_commit_message`).
