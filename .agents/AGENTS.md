# Инструкции агента — ESPHome Local MCP Server

Этот документ является единым и авторитетным источником инструкций, описывающим архитектуру проекта, ключевые особенности ESPHome Device Builder API и критические правила разработки.

---

## 📖 Официальная документация API

При любой модификации `server.py` или логики взаимодействия с ESPHome Device Builder API **обязательно** обращайтесь к официальной справочной документации:

**ESPHome Device Builder API Reference:**
`https://github.com/esphome/device-builder/blob/main/docs/API.md`

Документация описывает:
- Все доступные WebSocket-команды (`devices/*`, `firmware/*`, `boards/*` и др.)
- Форматы запросов (`CommandMessage`) и ответов (`ResultMessage`, `EventMessage`, `ErrorMessage`)
- Коды ошибок (`ErrorCode`) и их значения
- Поведение стриминговых команд (streaming `output` → `result`)
- Двухшаговые цепочки задач (`firmware/install` = compile job + upload job)
- Аутентификацию (`requires_auth`, `auth/login`)

---

## 🏗 Архитектура сервера и ключевые технологии

- **Транспорт:** `stdio` (стандартный ввод-вывод) — стандарт для локальных MCP-серверов.
- **Фреймворк:** `mcp.server.fastmcp.FastMCP` (официальный Python SDK).
- **Стек:** Python 3.11+, библиотека `websockets`, `tomllib` (стандартная библиотека) / `tomli`. Окружение — Python venv (`.venv/`).
- **Связь с ESPHome:** WebSocket `ws://<host>:<port>/ws` или `wss://<host>:<port>/ws` (при SSL).
- **Конфигурация:** Настройки хоста, порта и SSL считываются из файла `.env` в формате TOML (`host`, `port`, `ssl`). При отсутствии файла или ошибках парсинга используется fallback на `localhost` и порт `6052`.<br>Параметр `ssl = true` принудительно включает `wss://` (WebSocket Secure) — необходим при подключении через Traefik/HTTPS. Если `ssl` не задан — автоопределение: `wss://` для портов 443/8443, иначе `ws://`.
- **Логирование:** Строго в `sys.stderr` — стандартный вывод `stdout` зарезервирован исключительно под передачу сообщений протокола MCP.

---

## 🛠 Доступные MCP Инструменты (15 инструментов)

1. **Базовые операции (Compile & Flash):**
   - `validate_yaml(configuration, host, port)` — быстрая валидация YAML (`devices/validate`)
   - `compile_firmware(configuration, host, port)` — компиляция прошивки без загрузки (`firmware/compile`)
   - `flash_ota(configuration, host, port)` — OTA-прошивка готового бинарника (`firmware/upload`)
   - `compile_and_flash(configuration, host, port)` — полный цикл сборки и OTA-прошивки (`firmware/install`)

2. **Мониторинг и отладка (P0):**
   - `list_devices(host, port)` — полный список устройств и их статусов (`devices/list`)
   - `stream_device_logs(configuration, port, duration_seconds, lines_count, host, api_port)` — чтение логов в реальном времени (`devices/logs`)
   - `decode_crash_backtrace(configuration, lines, host, port)` — расшифровка C++ дампов паники (`devices/decode_backtrace`)
   - `search_yaml_configs(query, context_lines, case_sensitive, host, port)` — поиск подстроки по всем YAML-конфигурациям (`yaml/search`)

3. **Управление конфигурациями, платами и сборками (P1):**
   - `manage_device_config(action, configuration, content, new_name, allow_wipe, host, port)` — CRUD операций с YAML (`devices/*`)
   - `get_board_info(action, board_id, platform, query, limit, host, port)` — информация о платах и их совместимости (`boards/*`)
   - `manage_build_jobs(action, configuration, job_id, status_filter, host, port)` — управление очередью компиляции и кэшем (`firmware/*`)

4. **Пакетные операции, архивация и безопасность (P2):**
   - `batch_compile_and_flash(configurations, action, port, host, api_port)` — пакетная компиляция и прошивка (`firmware/*_bulk`)
   - `archive_devices(action, configuration, host, port)` — архивация и восстановление устройств (`devices/*_archived`)
   - `manage_device_labels(configuration, label_ids, host, port)` — управление метками устройств (`devices/set_labels`)
   - `authenticate_esphome(username, password, token, host, port)` — аутентификация по паролю/токену (`auth/login`)

---

## 📁 Правила работы с путями (`configuration`)

Все инструменты, принимающие параметр `configuration`, работают исключительно через ESPHome WebSocket API без доступа к локальным файлам хоста:

| Формат | Пример | Поведение |
|--------|--------|-----------|
| **Имя файла/конфигурации** | `mcp-test.yaml` | Передаётся напрямую в ESPHome API |
| **Относительный с префиксом** | `config/mcp-test.yaml` | Префикс `config/` обрезается автоматически, передаётся `mcp-test.yaml` |

---

## 🧪 Тестирование и манифест конфигураций

В проекте реализован адаптивный тестовый сценарий ([`test/test_mcp_server.py`](file:///Users/andreyzolotnitskiy/Documents/github/ESPHomeLocalMCP/test/test_mcp_server.py)):
- **Манифест устройств ([`test/test_devices.json`](file:///Users/andreyzolotnitskiy/Documents/github/ESPHomeLocalMCP/test/test_devices.json)):** содержит документированный список целевых устройств (`test.yaml`, `liligo-t-internet.yaml`, `esp32-c6-lora-test-01.yaml`) и флаги разрешённых операций (`allow_compile`, `allow_ota_flash`, `allow_state_mutation`).
- **Режимы тестирования:**
  - *Full Pipeline:* при наличии и валидности манифеста запускается полный комплекс тестов (включая компиляцию и OTA-прошивку).
  - *Safe Mode:* при отсутствии манифеста или ошибках его парсинга автоматически выполняются только безопасные read-only тесты, не меняющие состояние устройств.
- **Запуск тестов:** `.venv/bin/python3 test/test_mcp_server.py`

---

## 🔴 Критические правила разработки и предотвращения ошибок

При внесении любых изменений в код `server.py` или взаимодействия с API **строго соблюдайте следующие правила**:

### 1. Имена событий (Events) в потоковых командах
- При выполнении прямых потоковых команд (`firmware/follow_job`, `devices/validate`), ESPHome API возвращает события с именами **`"output"`** (для строк лога) и **`"result"`** (для статуса завершения).
- События `job_output`, `job_completed`, `job_failed` используются **только** в глобальной шине событий `subscribe_events`. Не путайте их!

### 2. Авторизация и первое сообщение (ServerInfoMessage)
- Сразу после установления WebSocket соединения сервер ESPHome отправляет `ServerInfoMessage`.
- Обязательно считайте его (`await asyncio.wait_for(ws.recv(), timeout=2.0)`) и проверьте поле `requires_auth`.
- Если `requires_auth` равно `True`, необходимо проверить наличие авторизации, иначе запросы упадут с ошибкой `not_authenticated`.

### 3. Явная передача параметров OTA-прошивки
- В командах `firmware/upload` и `firmware/install` для гарантированной прошивки "по воздуху" всегда явно передавайте параметр `"port": "OTA"` внутри словаря `args`.

### 4. Логирование и стандартный вывод (stdout)
- В MCP-сервере, использующем `stdio` транспорт, стандартный вывод (`stdout`) зарезервирован исключительно под передачу JSON-RPC сообщений протокола.
- Вызовы `print()` **категорически запрещены**. Все логирование направляется строго в **`sys.stderr`** (`logging.basicConfig(stream=sys.stderr)`).

### 5. ⚠️ Обязательная актуализация документации
- **При любых изменениях в коде, функционале, параметрах инструментов, архитектуре или конфигурации в обязательном порядке актуализировать документацию проекта (`README.md`, `.agents/AGENTS.md`).**

---

## 💡 Особенности поведения ESPHome API

- `devices/validate` — стриминговая команда: ожидай события `output` и `result`.
- `firmware/compile`, `firmware/upload`, `firmware/install` — двухшаговые: сначала получи `job_id`, затем подпишись на `firmware/follow_job`.
- `firmware/clean`, `firmware/reset_build_env` — также возвращают `job_id`, требуют подписки на `firmware/follow_job`.
- `devices/logs` — стриминговая команда; при сборе логов ограничивай время через `asyncio.wait_for(..., timeout)`, так как поток логов не завершается сам по себе.
- `devices/decode_backtrace` — синхронный ответ; возвращает `unavailable_reason`, если расшифровка невозможна.
- `yaml/search` — синхронный ответ с массивом результатов; пустая строка запроса возвращает `[]`.
- `boards/get_board` — структура пинов: поля `gpio`, `label`, `features[]`, `notes`, `available` (`false` = занят SPI Flash и т.д.).
- `firmware/compile_bulk`, `firmware/install_bulk` — возвращают массив `[FirmwareJob]` немедленно; статус отслеживается через `firmware/follow_job`.
- `devices/create` — возвращает **реальное** имя созданного файла в `result.configuration`. ESPHome может slugify имя (убирать спецсимволы), всегда используйте значение из ответа API.
