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

## 🤖 Документация для агентов и руководство по интеграции

- **Руководство для AI-агентов:** [`docs/ESPHOME_AGENT_GUIDE.md`](file:///Users/andreyzolotnitskiy/Documents/github/ESPHomeLocalMCP/docs/ESPHOME_AGENT_GUIDE.md) — Золотые правила, 7 сценариев взаимодействия и справочник 29 инструментов.
- **Инструкция по интеграции в проекты:** [`docs/USER_INTEGRATION_GUIDE.md`](file:///Users/andreyzolotnitskiy/Documents/github/ESPHomeLocalMCP/docs/USER_INTEGRATION_GUIDE.md) — Подключение к Cursor, Antigravity, VS Code Copilot, Claude Desktop, Windsurf, Aider.

---

## 🏗 Архитектура сервера и ключевые технологии

- **Транспорт:** `stdio` (стандартный ввод-вывод) — стандарт для локальных MCP-серверов.
- **Фреймворк:** `mcp.server.fastmcp.FastMCP` (официальный Python SDK).
- **Стек:** Python 3.11+, библиотека `websockets`, `tomllib` (стандартная библиотека) / `tomli`. Окружение — Python venv (`.venv/`).
- **Связь с ESPHome:** WebSocket `ws://<host>:<port>/ws` или `wss://<host>:<port>/ws` (при SSL).
- **Конфигурация:** Настройки хоста, порта и SSL считываются из файла `.env` в формате TOML (`host`, `port`, `ssl`). При отсутствии файла или ошибках парсинга используется fallback на `localhost` и порт `6052`.<br>Параметр `ssl = true` принудительно включает `wss://` (WebSocket Secure) — необходим при подключении через Traefik/HTTPS. Если `ssl` не задан — автоопределение: `wss://` для портов 443/8443, иначе `ws://`.
- **Логирование:** Строго в `sys.stderr` — стандартный вывод `stdout` зарезервирован исключительно под передачу сообщений протокола MCP.

---

## 🛠 Доступные MCP Инструменты (29 инструментов)

1. **Базовые и защищенные операции (Compile, Flash & Canary Rollback):**
   - `validate_yaml(configuration, host, port)` — быстрая валидация YAML (`devices/validate`)
   - `compile_firmware(configuration, force_local, host, port)` — компиляция прошивки без загрузки (`firmware/compile`)
   - `flash_ota(configuration, port, bootloader, host, api_port)` — прошивка готового бинарника (OTA / IP / Serial, `firmware/upload`)
   - `compile_and_flash(configuration, port, force_local, bootloader, host, api_port)` — полный цикл сборки и прошивки (`firmware/install`)
   - `flash_canary_ota(configuration, port, compile_first, monitor_seconds, auto_rollback, force_local, bootloader, host, api_port)` — защищенная двухэтапная Canary OTA-прошивка с baseline-снимком YAML, сторожевым мониторингом запуска (Canary Watchdog), выявлением паники ядра/бутлупа и экстренным авто-откатом (Auto-Rollback) (`devices/get_config`, `firmware/*`, `devices/logs`, `devices/decode_backtrace`)

2. **Мониторинг, отладка, сетевая диагностика и аппаратный аудит (P0/P2/Phase 5):**
   - `list_devices(host, port)` — полный список устройств и их статусов (`devices/list`)
   - `stream_device_logs(configuration, port, duration_seconds, lines_count, no_states, host, api_port)` — чтение логов в реальном времени с подавлением шума состояний (`devices/logs`)
   - `decode_crash_backtrace(configuration, lines, host, port)` — расшифровка C++ дампов паники (`devices/decode_backtrace`)
   - `search_yaml_configs(query, context_lines, case_sensitive, host, port)` — поиск подстроки по всем YAML-конфигурациям (`yaml/search`)
   - `troubleshoot_device(configuration, action, host, port)` — глубокая сетевая диагностика доступности узла (DNS, mDNS, ICMP Ping RTT) и матриц статусов (`devices/troubleshoot`, `devices/get_states`)
   - `validate_hardware_pinout(configuration, content, host, port)` — интеллектуальный статический и аппаратный аудит GPIO: детекция конфликтов SPI Flash, входных пинов (Input-only), повторного использования пинов, коллизий адресов I2C, предупреждения Strapping/ADC2 пинов (`devices/get_config`, `boards/get_board`)

3. **Управление конфигурациями, секретами, метками, AST автоматизациями, платами и сборками (P1/P2):**
   - `manage_device_config(action, configuration, content, new_name, board_id, friendly_name, comment, ssid, psk, config_only, overwrite, allow_wipe, fields, sub_entities, expected, apply, project_name, ignore_device, host, port)` — CRUD операций с YAML, генерация по шаблону платы, получение ключа шифрования (`action="key"`), вставка компонентов (`action="add_component"` dry-run/apply), оптимистичная блокировка (`expected` CAS), обновление метаданных sidecar JSON (`action="metadata"`), импорт (`action="import"`), сокрытие (`action="ignore"`), secrets, офлайн/онлайн переименование (`devices/*`)
   - `migrate_device_config(configuration, content, apply, host, port)` — автоматическая миграция устаревшего YAML синтаксиса ESPHome (`editor/migrate_config`)
   - `manage_automations(action, configuration, component_id, trigger, kind, automation, expected, apply, query, host, port)` — управление AST автоматизаций ESPHome: парсинг логики (`parse`), доступные сущности (`available`), каталоги триггеров/действий/условий, световые эффекты (`light_effects`), фильтры сенсоров (`filters`), паспорта схем (`docs`/`bodies`), точечное добавление (`upsert`) и удаление (`delete`) блоков с серверным сохранением (`save: true`) и оптимистичной блокировкой (`expected`) (`automations/*`)
   - `search_components(action, query, category, platform, component_id, limit, offset, host, port)` — каталог и паспорта компонентов ESPHome (>940 записей), схема `config_entries`, зависимости, шины, категории и режимы пинов (`components/*`, `components/get_component_bodies`)
   - `manage_secrets(action, key, value, ssid, psk, overwrite, host, port)` — безопасное управление секретами (`secrets.yaml`), просмотр доступных ключей, запись секретов с флагом `overwrite`, настройка Wi-Fi (`config/*`)
   - `manage_labels(action, label_id, name, color, host, port)` — управление глобальным каталогом меток (тегов) парка устройств (`labels/*`)
   - `batch_manage_devices(action, configurations, label_ids, updates, host, port)` — пакетные операции: массовая архивация, массовое удаление и массовое назначение меток (`devices/*_bulk`)
   - `get_host_info(action, preferences, host, port)` — версии бэкенда/ESPHome Core, список физических USB-Serial портов, чтение и запись глобальных настроек дашборда (`config/*`)
   - `get_board_info(action, board_id, platform, variant, mcu, tag, query, limit, offset, host, port)` — информация о платах, поиск по чипам/тегам, пагинация и распиновка (`boards/*`)
   - `manage_build_jobs(action, configuration, job_id, status_filter, host, port)` — управление очередью сборки, кэшем, очистка истории задач (`clear`), сброс отложенных обновлений (`clear_queued`), детальное профилирование расхода памяти RAM/Flash (`analyze_memory`) (`firmware/*`)

4. **Пакетные операции сборки, архивация, Git-история, кластерная сборка, безопасность и версия (P2):**
   - `batch_compile_and_flash(configurations, action, port, force_local, bootloader, host, api_port)` — пакетная компиляция и прошивка (`firmware/*_bulk`)
   - `get_firmware_binaries(configuration, action, file, save_path, host, port)` — инспекция артефактов прошивки (`list`), генерация токенов скачивания (`token`) и скачивание бинарников (`download`) через HTTP
   - `archive_devices(action, configuration, host, port)` — архивация и восстановление устройств (`devices/*_archived`)
   - `manage_device_labels(configuration, label_ids, host, port)` — управление метками одного устройства (`devices/set_labels`)
   - `manage_version_history(action, configuration, sha, sha_compare, max_count, config_dir, host, port)` — нативные WebSocket-команды истории версий (`version_history/*`: `list_versions`, `get_diff`, `get_version`, `list_deleted`, `restore`) с автоматическим Git fallback
   - `manage_remote_build(action, enabled, cleanup_ttl_seconds, hostname, target_port, pairing_key, peer_id, dashboard_id, pin_sha256, receiver_label, offloader_label, open_window, version_match_policy, include_local_in_pool, remote_builds_enabled, host, port)` — управление распределенной кластерной компиляцией (`remote_build/*`), identity узла, Noise XX сопряжение, окно сопряжения (`set_pairing_window`), ротация ключей (`rotate_identity`), диспетчер сборок (`set_offloader_settings`)
   - `authenticate_esphome(action, username, password, token, host, port)` — аутентификация по паролю/токену (`login`), отзыв токенов (`logout`) и продление сессии (`refresh`) (`auth/*`)
   - `get_server_version()` — получение информации о версии сервера и протоколе (SemVer)

5. **Контекстные ресурсы MCP (4 ресурса, Фаза 4.1):**
   - `esphome://devices` — живой реестр всех устройств ESPHome, их IP-адресов, онлайн/офлайн статусов и версий (`devices/list` + `devices/get_states`)
   - `esphome://configs/{configuration}` — прямое считывание YAML-конфигурации устройства в контекст LLM (`devices/get_config`)
   - `esphome://components/{component_id}` — паспорт компонента, схема параметров `config_entries`, шины (`components/get_component_bodies`)
   - `esphome://boards/{board_id}` — паспорт платы, аппаратная распиновка GPIO (pinout), strapping/Flash ограничения (`boards/get_board`)

6. **Интерактивные стандартизированные промпты MCP (3 промпта, Фаза 4.2):**
   - `new_device_wizard` — интерактивный мастер проектирования и создания нового устройства (аудит платы, шины, конфиг, валидация)
   - `troubleshoot_offline_device` — автономный диагностический чеклист расследования причин недоступности узла (зонд, Git-diff, паника ядра)
   - `safe_firmware_upgrade` — регламент безопасного обновления прошивки без риска потери связи с узлом (откат, валидация, RAM/Flash аудит, OTA)

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
- **Манифест устройств ([`test/test_devices.json`](file:///Users/andreyzolotnitskiy/Documents/github/ESPHomeLocalMCP/test/test_devices.json)):** содержит строго документированный список разрешенных целевых устройств (`test.yaml`, `liligo-t-internet.yaml`, `r5-gate-01.yaml`) и флаги разрешённых операций (`allow_compile`, `allow_ota_flash`, `allow_state_mutation`). Использование любых других конфигураций строго запрещено.
- **Режимы тестирования:**
  - *Full Pipeline:* при наличии и валидности манифеста запускается полный комплекс тестов (включая компиляцию и OTA-прошивку разрешенных устройств).
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

### 3. Передача целевого порта и флагов прошивки
- В командах `firmware/upload` и `firmware/install` по умолчанию передается `"port": "OTA"` внутри словаря `args` для гарантированной прошивки "по воздуху". Также поддерживается явный IP-адрес/hostname или Serial-порт.
- Сетевой порт сервера ESPHome задается параметром `api_port` (по умолчанию `6052`).
- Опциональные флаги `force_local` и `bootloader` передаются в `args` только при установке в `True`.

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

---

## 🔖 Версионирование проекта (SemVer & Changelog)

Проект строго придерживается стандарта **[Semantic Versioning 2.0.0](https://semver.org/lang/ru/)**:
- **Single Source of Truth:** файл [`__version__.py`](file:///Users/andreyzolotnitskiy/Documents/github/ESPHomeLocalMCP/__version__.py) (`__version__ = "X.Y.Z"`).
- **Правила изменения версии:**
  - `MAJOR` (X.0.0) — обратно-несовместимые изменения сигнатур инструментов или формата ответа.
  - `MINOR` (1.X.0) — добавление новых инструментов, параметров или возможностей без нарушения совместимости.
  - `PATCH` (1.0.X) — исправление ошибок, оптимизация или обновление документации.
- **Журнал изменений:** все изменения обязательно фиксируются в [`CHANGELOG.md`](file:///Users/andreyzolotnitskiy/Documents/github/ESPHomeLocalMCP/CHANGELOG.md) в секции `[Unreleased]`.
- **Скрипт бампа:** для автоматического обновления версий, README, CHANGELOG и git-тегов используйте:
  `.venv/bin/python3 scripts/bump_version.py patch|minor|major`


