# 📝 Журнал изменений (Changelog)

Все заметные изменения в проекте **ESPHome Local MCP Server** документируются в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.0.0/),
и проект придерживается [Семантического версионирования (Semantic Versioning 2.0.0)](https://semver.org/lang/ru/).

---

## [Unreleased]

---

## [1.2.0] - 2026-08-21

### Added
- **Расширение управления конфигурациями (Пункт 1.3 ROADMAP):**
  - Поддержка создания устройств из типовых шаблонов плат (`board_id`) без необходимости ручной передачи полного YAML-контента.
  - Интеграция с `secrets.yaml`: автоматическое сохранение реквизитов Wi-Fi (`ssid`, `psk`) при создании конфигурации через `!secret wifi_ssid`.
  - Поддержка онлайн двухшагового переименования устройства (`devices/rename` с `config_only=False`: сборка → OTA-прошивка старого адреса → атомарная смена файлов) с подпиской на стрим логов.
  - Добавлены параметры `board_id`, `friendly_name`, `ssid`, `psk`, `config_only`, `overwrite` в `manage_device_config`.
  - Реализован изолированный тест жизненного цикла `test_device_config_crud_lifecycle` с гарантированной очисткой (Teardown Guard).

---
## [1.1.0] - 2026-08-21

### Added
- **Расширение параметров прошивки и компиляции (Пункт 1.2 ROADMAP):**
  - Поддержка гибкого выбора целевого порта/адреса устройства (`port: "OTA"` по умолчанию, явный IP-адрес или Serial-порт) в `flash_ota`, `compile_and_flash`, `batch_compile_and_flash`.
  - Поддержка принудительной локальной сборки без кэша/offloading (`force_local: bool = False`) в `compile_firmware`, `compile_and_flash`, `batch_compile_and_flash`.
  - Поддержка прошивки образа загрузчика (`bootloader: bool = False`) в `flash_ota`, `compile_and_flash`, `batch_compile_and_flash`.
  - Разделение параметров сетевого порта ESPHome сервера (`api_port: int = DEFAULT_PORT`) и целевого порта устройства (`port: str = "OTA"`).
- Добавлена дорожная карта развития проекта ([`ROADMAP.md`](ROADMAP.md)) с чекбоксами для отслеживания задач и интеграции с официальным API.

---
## [1.0.0] - 2026-08-21

### Added
- **Архитектура и интеграция:**
  - Интеграция с локальным ESPHome Device Builder API через WebSocket (`ws://<host>:<port>/ws` и `wss://<host>:<port>/ws`).
  - Поддержка чтения хоста, порта и SSL из файла `.env` в формате TOML с fallback на `localhost:6052`.
  - Транспорт `stdio` (FastMCP) с защитой стандартного вывода (все логирование направляется исключительно в `sys.stderr`).
  - Автоматическая очистка ANSI-escape кодов из логов и вывода сборщика.
  - Нормализация путей `configuration` (поддержка `foo.yaml` и `config/foo.yaml`).

- **16 MCP-инструментов:**
  - `validate_yaml` — быстрая валидация YAML синтаксиса (`devices/validate`).
  - `compile_firmware` — компиляция прошивки без загрузки (`firmware/compile`).
  - `flash_ota` — OTA-прошивка готового бинарника (`firmware/upload`).
  - `compile_and_flash` — полный цикл сборки и OTA-прошивки (`firmware/install`).
  - `list_devices` — список устройств с IP-адресами, статусами, версиями и метками (`devices/list`).
  - `stream_device_logs` — чтение runtime-логов по OTA/Serial в реальном времени (`devices/logs`).
  - `decode_crash_backtrace` — расшифровка C++ стектрейсов/дампов паники (`devices/decode_backtrace`).
  - `search_yaml_configs` — полнотекстовый поиск по всем YAML-файлам (`yaml/search`).
  - `manage_device_config` — CRUD операции с конфигурациями (`get`, `update`, `create`, `rename`, `delete`).
  - `get_board_info` — поиск плат, распиновка, параметры железа и совместимость (`boards/*`).
  - `manage_build_jobs` — очередь компиляции, отмена, очистка сборки и сброс `.esphome/` (`firmware/*`).
  - `batch_compile_and_flash` — пакетная компиляция и OTA-прошивка с отложенными обновлениями (`firmware/*_bulk`).
  - `archive_devices` — архивация (soft-delete), восстановление и просмотр архива (`devices/*_archived`).
  - `manage_device_labels` — назначение и удаление меток устройств (`devices/set_labels`).
  - `authenticate_esphome` — аутентификация по логину/паролю или токену (`auth/login`).
  - `get_server_version` — информация о версии MCP-сервера и параметрах среды.

- **Версионирование и тестирование:**
  - Модуль [`__version__.py`](__version__.py) — единый источник правды (SSOT) для версии проекта.
  - Скрипт автоматизации бампа версий [`scripts/bump_version.py`](scripts/bump_version.py).
  - Адаптивный тестовый фреймворк [`test/test_mcp_server.py`](test/test_mcp_server.py) с поддержкой безопасного режима (Safe Mode) и манифеста устройств.
