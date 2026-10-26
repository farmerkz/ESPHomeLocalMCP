# 📝 Журнал изменений (Changelog)

Все заметные изменения в проекте **ESPHome Local MCP Server** документируются в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.0.0/),
и проект придерживается [Семантического версионирования (Semantic Versioning 2.0.0)](https://semver.org/lang/ru/).

---

## [Unreleased]

---

## [1.18.0] - 2026-10-26

### Added (Актуализация API 2026.10 — Этап 4: Signed OTA, Friendly Name & Device Components)
- **Точечное обновление дружественного имени в YAML (`manage_device_config(action="edit_friendly_name")`)**:
  - Поддержка новой команды WebSocket API `devices/edit_friendly_name` (`{configuration, new_friendly_name}`).
  - Атомарная перезапись поля `esphome.friendly_name` непосредственно в YAML-файле без запуска фоновых задач сборки и прошивки (с возвратом статуса `rewritten`).
- **Атомарное переименование с обновлением `friendly_name` (`manage_device_config(action="rename")`)**:
  - Поддержка нового параметра `new_friendly_name` (или `friendly_name`) в команде `devices/rename` для одновременного переименования файла/hostname и отображаемого имени в одной цепочке записи.
- **Защита цифровой подписи прошивки (Hardware & Signed OTA Verification Safety)**:
  - В инструменты `flash_ota`, `compile_and_flash` и `flash_canary_ota` добавлен пре-чек флагов `runtime_state.ota_signed` и `ota_signing_key` через `evaluate_signed_ota_safety`.
  - Автоматическая блокировка небезопасной сетевой OTA-прошивки в случае, когда микроконтроллер вещает `ota_signed=True`, но в YAML-конфигурации отсутствует ключ подписи (`ota_signing_key=False`), с рекомендацией прошивки по USB/Serial или добавления `signing_key`.
  - Отображение статуса `Signed OTA` в выводе `list_devices`.
- **Инспекция компонентов конкретного устройства (`search_components(action="device_components")`)**:
  - Новый режим `action="device_components"` (и алиас `"config_components"`) с параметром `configuration` в инструменте `search_components` (аналог `get_config_components`).
  - Автоматический сбор используемых устройством компонентов из метаданных сканера (`component_ids`, `loaded_platforms`, `loaded_integrations`) и прямого парсинга YAML (`extract_yaml_component_ids`) с обогащением описаниями, зависимостями и ссылками на документацию из каталога `components/get_component_bodies`.

### Fixed
- **Локальная валидация в `manage_automations(action="docs")`**:
  - Проверка обязательного параметра `refs` вынесена до открытия WebSocket-соединения; добавлена поддержка пагинированного ответа `{bodies, remaining}` в `automations/get_bodies`.
- **Документация и тесты**:
  - Исправлены ссылки на корневой `AGENTS.md` в `ROADMAP.md`.
  - Тестовый набор в `test/test_mcp_server.py` расширен до 25 модульных и 37 интеграционных тестов (всего 62 теста).

---
## [1.17.0] - 2026-10-02

### Added (Фаза 5.3.1: Canary OTA-прошивка с авто-откатом при Bootloop)
- **Новый инструмент `flash_canary_ota` (Инструмент №29)**:
  - Интеллектуальный защищенный конвейер Canary-прошивки для устройств ESPHome.
  - Автоматическое снятие контрольного снимка (Baseline Snapshot) текущего рабочего YAML через `devices/get_config` до отправки изменений на устройство.
  - Контролируемый процесс сборки (`compile_first`) и OTA-загрузки: при сетевых сбоях или ошибках PlatformIO сборка безопасно останавливается без риска повреждения прошивки узла.
  - Сторожевой мониторинг первого запуска (Canary Watchdog): прослушивание runtime-логов устройства в течение настраиваемого времени `monitor_seconds` с фильтрацией шума `no_states=True`.
  - Детекция фатальных аппаратных аварий ядра и бутлупа:
    - `Guru Meditation Panic` и стектрейсы `Backtrace`.
    - Аварийные перезагрузки по сторожевому таймеру (`TG0WDT_SYS_RESET`, `RTCWDT_RTC_RESET`).
    - Критические отказы `abort() was called` и `assertion failed`.
    - Циклическая перезагрузка (Bootloop Detection, >2 рестартов за период мониторинга).
    - Переход устройства в аварийный режим ESPHome Safe Mode.
    - Повреждения динамической памяти (Heap Corruption).
  - Автоматический откат прошивки (Auto-Rollback):
    - Немедленное восстановление исходной рабочей конфигурации из сохраненного baseline-снимка.
    - Экстренная компиляция стабильной прошивки.
    - Экстренная OTA-прошивка узла через технологическое окно ESPHome Safe Mode.
    - Автоматическая расшифровка дампов паники `Backtrace` через `devices/decode_backtrace`.
- **Тестирование**:
  - В `test/test_mcp_server.py` добавлен модульный тест `test_analyze_canary_health_unit` (проверка детекции всех классов паники, WDT сбросов, Safe Mode и нормального лога).
  - Добавлен интеграционный тест `test_flash_canary_ota_integration` (проверка безопасной остановки при ошибках компиляции и Canary-прошивка со сторожевым мониторингом `test.yaml`).

---

## [1.16.0] - 2026-10-02

### Added (Фаза 5.1.1: Аппаратный аудит распиновки)
- **Новый инструмент `validate_hardware_pinout` (Инструмент №28)**:
  - Полный статический и аппаратный аудит использования GPIO и шин интерфейсов в YAML-конфигурациях ESPHome.
  - Поддержка проверки как существующих конфигураций (`configuration`), так и текста "на лету" (`content`) до сохранения на диск.
  - Детекция критических аппаратных конфликтов (**FATAL**):
    - Попытка назначения пинов встроенной SPI Flash памяти (GPIO6–GPIO11 на ESP32/ESP8266, GPIO12–17 на ESP32-C3, GPIO26–32 на ESP32-S3), приводящих к аппаратному сбою шины Flash и циклической перезагрузке (bootloop).
    - Назначение физически входных пинов (Input-only, GPIO34, 35, 36, 39 на ESP32) на роли вывода (`OUTPUT`, `PWM`, шины `I2C`).
    - Коллизия повторного использования одного и того же GPIO несколькими компонентами схемы.
    - Аппаратная коллизия адресов устройств на шине I2C (дублирование одинаковых адресов без мультиплексора TCA9548A).
  - Предостережения и диагностические рекомендации (**WARNINGS**):
    - Использование Strapping пинов (GPIO0, GPIO2, GPIO12, GPIO15), влияющих на выбор режима загрузки и напряжение Flash при подаче питания.
    - Конфликт аналого-цифрового преобразователя ADC2 с передатчиком Wi-Fi при одновременной работе.
  - Интеграция с официальной базой распиновок плат ESPHome (`boards/get_board`) для обогащения отчета аппаратными примечаниями и спецификациями.
- **Тестирование**:
  - В `test/test_mcp_server.py` добавлены модульный тест `test_validate_hardware_pinout_unit` (проверка детекции всех классов фатальных ошибок и корректных конфигураций) и интеграционный тест `test_validate_hardware_pinout_integration` (аудит конфигураций разрешенных манифестом устройств `test.yaml`, `liligo-t-internet.yaml`, `r5-gate-01.yaml`).

---

## [1.15.0] - 2026-10-02

### Added (Фаза 4: MCP Resources & Prompts)
- **Динамические и контекстные ресурсы MCP (`@mcp.resource`)**:
  - `esphome://devices` (MIME: `application/json`) — актуальный живой реестр всех устройств ESPHome с их IP-адресами, статусом онлайн/офлайн (обогащенный данными `devices/get_states`), версиями прошивок и платформами.
  - `esphome://configs/{configuration}` (MIME: `text/yaml`) — прямое считывание исходного YAML-кода конфигураций устройств по URI для передачи в контекст AI-клиента без затрат на tool call.
  - `esphome://components/{component_id}` (MIME: `application/json`) — получение детальной схемы настроек `config_entries`, требований к шинам и зависимостей компонентов через `components/get_component_bodies`.
  - `esphome://boards/{board_id}` (MIME: `application/json`) — получение аппаратной распиновки (pinout), доступных пинов и ограничений плат контроллеров через `boards/get_board`.
- **Интерактивные стандартизированные промпты MCP (`@mcp.prompt`)**:
  - `new_device_wizard` — пошаговый мастер проектирования нового устройства (выбор платы, проверка шин, датчиков, валидация синтаксиса и сборка).
  - `troubleshoot_offline_device` — автономный диагностический чеклист для расследования причин недоступности узла (зонд доступности `probe`, история версий Git `get_diff`, логи и дампы паники).
  - `safe_firmware_upgrade` — регламент безопасного обновления прошивки (проверка доступности, контрольная точка отката, валидация YAML, профилирование памяти, OTA-прошивка и пост-мониторинг).
- **Тестирование**:
  - В `test/test_mcp_server.py` добавлены тесты `test_mcp_resources` и `test_mcp_prompts`, проверяющие регистрацию и чтение ресурсов через FastMCP `ResourceManager` и рендеринг промптов через `PromptManager`.

---

## [1.14.0] - 2026-10-02

### Added & Enhanced (Этап 3: P2 Platform актуализации API)
- **`manage_device_config`**:
  - Добавлено действие `action="metadata"` (`devices/update`) для обновления отображаемого имени (`friendly_name`), комментария (`comment`) и платы (`board_id`) в метаданных sidecar JSON без перезаписи и компиляции YAML.
  - Добавлено действие `action="import"` (`devices/import`) для импорта и принятия обнаруженного узла в постоянный реестр ESPHome.
  - Добавлено действие `action="ignore"` (`devices/ignore`) для управления видимостью устройств в интерфейсе дашборда (`ignore_device`).
- **`get_host_info`**:
  - Добавлены действия `action="preferences"` (`config/get_preferences`) и `action="set_preferences"` (`config/set_preferences`) с параметром `preferences: dict` для инспекции и настройки глобальных параметров дашборда (тема оформления, режим отображения карточек, автоматическая Git-история версий `version_history_enabled`, раскладки редакторов).
- **`authenticate_esphome`**:
  - Добавлен параметр `action: str = "login"` с поддержкой действий `"logout"` (`auth/logout`) для отзыва токенов текущей сессии и `"refresh"` (`auth/refresh`) для безопасного продления времени жизни токена.
- **`manage_remote_build`**:
  - Добавлено действие `action="set_pairing_window"` (`remote_build/set_pairing_window`) с параметром `open_window: bool` для управления окном сопряжения Noise XX.
  - Добавлено действие `action="rotate_identity"` (`remote_build/rotate_identity`) для безопасной смены X25519 ключей и fingerprint узла.
  - Добавлено действие `action="set_offloader_settings"` (`remote_build/set_offloader_settings`) с параметрами `remote_builds_enabled`, `version_match_policy`, `include_local_in_pool` для управления диспетчером удаленной сборки.
  - Добавлено действие `action="set_pairing_enabled"` (`remote_build/set_pairing_enabled`) для селективного включения/отключения маршрутизации сборок на узел.
- **Тестирование**:
  - Расширен тестовый набор `test/test_mcp_server.py` тестами чтения/записи `preferences`, `auth/logout/refresh`, расширенных команд `remote_build` и управления метаданными `metadata`/`ignore` на разрешенных устройствах.

---

## [1.13.0] - 2026-10-02

### Added & Enhanced (Этап 2: P1 Extensions актуализации API)
- **`manage_device_config`**:
  - Добавлено действие `action="key"` (`devices/get_encryption_key`) для безопасного извлечения Native API / OTA ключей шифрования устройств (с маскированием в логах).
  - Добавлено действие `action="add_component"` (`devices/add_component`) для добавления компонентов в конфигурацию с поддержкой режимов dry-run предпросмотра (`apply=False`) и атомарного сохранения (`apply=True`).
  - Добавлен параметр `expected` для атомарной Compare-and-Swap (CAS) защиты от параллельных перезаписей при `action="update"`.
- **`manage_build_jobs`**:
  - Добавлено действие `action="analyze_memory"` (`firmware/analyze_memory`) для запуска PlatformIO компиляции и профилирования расхода Flash и RAM по каждому компоненту и библиотеке.
- **`manage_secrets`**:
  - Добавлен параметр `overwrite: bool = True` в `action="set"`, позволяющий безопасно предотвращать перезапись существующих секретов (`overwrite=False` / create-if-absent).
- **`stream_device_logs`**:
  - Добавлен параметр `no_states: bool = False`, подавляющий избыточный шум регулярных обновлений состояний компонентов в потоке логов.
- **`manage_automations`**:
  - Добавлено действие `action="light_effects"` (`automations/get_light_effects`) — каталог световых эффектов с фильтрацией по платформам и поисковым запросам.
  - Добавлено действие `action="filters"` (`automations/get_filters`) — каталог фильтров для сенсоров и бинарных сенсоров.
  - Добавлено действие `action="docs"` (`automations/get_bodies`) — получение детальных паспортов и спецификаций схем параметров `config_entries` для триггеров, действий, условий, эффектов и фильтров.
- **Тестирование**:
  - Добавлены модульные и интеграционные тесты для всех новых возможностей; все 47 тестов успешно завершены (`Ran 47 tests in 37.975s, OK`).

---

## [1.12.1] - 2026-10-02

### Fixed & Changed (Этап 1: P0 Fixes актуализации API)
- **`manage_version_history`**:
  - Реализована нативная поддержка команд WebSocket API ESPHome (`version_history/list_versions`, `version_history/get_diff`, `version_history/get_version`, `version_history/list_deleted`, `version_history/restore`).
  - Сохранен надежный fallback на локальный CLI Git при работе в локальном dev-окружении, когда история отключена сервером ESPHome из-за внешнего репозитория.
- **`manage_remote_build`**:
  - Исправлены аргументы: для команд `approve_peer` и `remove_peer` теперь отправляется `dashboard_id`, для `unpair` отправляется `pin_sha256` (с сохранением fallback на `peer_id` для обратной совместимости).
  - В команду `request_pair` передаются `pin_sha256`, `receiver_label`, `offloader_label`; при отсутствии `pin_sha256` он автоматически запрашивается через предварительный вызов `preview_pair`.
  - Добавлено новое действие `action="get_identity"` для инспекции криптографической идентичности узла сборки (`dashboard_id`, `pin_sha256`, `server_version`, сетевые адреса и порт слушателя Noise).
- **`search_components`**:
  - В действии `action="get"` реализован запрос полной схемы компонента через команду `components/get_component_bodies` с выводом реестра параметров `config_entries` (типы данных, дефолтные значения, обязательность и флаги).
- **`manage_automations`**:
  - Точечное добавление (`upsert`) и удаление (`delete`) переведено на нативное сохранение бэкендом через аргумент `save: true` в API ESPHome.
  - Добавлен параметр `expected` для оптимистичной блокировки AST автоматизаций.
  - Удален дублирующий код локального наложения diff.
- **Тестирование и адаптивность**:
  - Тестовый сценарий приведен в строгое соответствие с манифестом: тесты мутаций проводятся исключительно на `test.yaml` с обязательным Teardown Guard; валидация и компиляция/OTA-прошивка выполняются на разрешенных `test.yaml`, `liligo-t-internet.yaml` и `r5-gate-01.yaml`.
  - 100% успешное прохождение всех 43 тестов (`Ran 43 tests in 32.640s - OK`).

---
## [1.12.0] - 2026-08-21

### Added
- **Управление распределенной кластерной компиляцией (Пункт 3.5 ROADMAP):**
  - Добавлен новый инструмент `manage_remote_build(action, enabled, cleanup_ttl_seconds, hostname, target_port, pairing_key, peer_id, host, port)` для управления кластерной сборкой и разгрузкой компиляции (Remote Build / Offloading, `remote_build/*` API).
  - Поддержка чтения конфигурации и списка пиров (`action="get_settings"`), динамического изменения параметров и TTL очистки сборок (`action="set_settings"`), предварительной проверки сопряжения с получением SHA-256 fingerprint публичного ключа (`action="preview_pair"`), отправки запроса на сопряжение (`action="request_pair"`), а также одобрения, разрыва сопряжения и удаления узлов (`action="approve_peer"`, `"unpair"`, `"remove_peer"`).
  - Добавлены модульные тесты сигнатуры и валидации, а также интеграционные тесты жизненного цикла параметров с Teardown Guard.
- **Полностью завершена Фаза 3 (P2) Дорожной карты!**
- **Документация для AI-агентов и интеграции в проекты:**
  - Создано исчерпывающее руководство для AI-агентов [`docs/ESPHOME_AGENT_GUIDE.md`](docs/ESPHOME_AGENT_GUIDE.md) с описанием 6 золотых правил, 7 типовых сценариев и каталогом всех 27 инструментов.
  - Создана подробная инструкция для пользователя [`docs/USER_INTEGRATION_GUIDE.md`](docs/USER_INTEGRATION_GUIDE.md) по подключению инструкций к Cursor, Antigravity, VS Code Copilot, Claude Desktop, Windsurf, Aider.

---
## [1.11.0] - 2026-08-21

### Added
- **Экспорт и скачивание скомпилированных артефактов прошивки (Пункт 3.4 ROADMAP):**
  - Добавлен новый инструмент `get_firmware_binaries(configuration, action, file, save_path, host, port)` для работы с бинарниками прошивок ESPHome (`firmware/get_binaries`, `firmware/download_token` API и HTTP GET `/api/firmware/download`).
  - Поддержка инспекции доступных артефактов на диске (`action="list"`), генерации безопасных одноразовых токенов и HTTP-ссылок скачивания (`action="token"`), а также прямого скачивания файла на диск хоста с расчетом размера и SHA-256 хеша (`action="download"`).
  - Добавлены модульные тесты сигнатуры и аргументов, а также интеграционные тесты скачивания с Teardown Guard.

---
## [1.10.0] - 2026-08-21

### Added
- **Управление AST автоматизаций ESPHome (Пункт 3.3 ROADMAP):**
  - Добавлен новый инструмент `manage_automations(action, configuration, component_id, trigger, kind, automation, apply, query, host, port)` для инспекции и редактирования AST автоматизаций (`automations/*` API).
  - Поддержка разбора структуры автоматизаций (`action="parse"`), получения доступных компонентов и скриптов (`action="available"`), просмотра глобальных каталогов (`action="triggers"`, `"actions"`, `"conditions"`), точечного добавления/обновления (`action="upsert"`) и удаления (`action="delete"`) блоков автоматизаций с поддержкой предпросмотра (Dry-Run) и автоматического применения (`apply=True`).
  - Добавлены модульные тесты валидации аргументов и интеграционные тесты жизненного цикла мутации AST с гарантированным Teardown Guard.

---
## [1.9.0] - 2026-08-21

### Added
- **История версий конфигураций и откат изменений (Пункт 3.2 ROADMAP):**
  - Добавлен новый инструмент `manage_version_history(action, configuration, sha, sha_compare, max_count, config_dir, host, port)` для работы с историей Git-коммитов конфигураций ESPHome.
  - Поддержка просмотра упорядоченной истории ревизий (`action="log"` / `"list"`), сравнения изменений (`action="diff"`), чтения архивного содержимого на выбранном коммите (`action="show"` / `"get"`), поиска удаленных ранее файлов (`action="deleted"` / `"list_deleted"`) и безопасного отката (`action="restore"`) через API ESPHome.
  - Добавлены модульные тесты сигнатуры и валидации, а также интеграционные тесты с полным жизненным циклом (create -> update -> restore -> teardown).

---
## [1.8.0] - 2026-08-21

### Added
- **Глубокая диагностика сети и доступности устройств (Пункт 3.1 ROADMAP):**
  - Добавлен новый инструмент `troubleshoot_device(configuration, action, host, port)` для сетевой диагностики и мониторинга доступности устройств (`devices/troubleshoot` и `devices/get_states` API).
  - Поддержка глубокой проверки узла (`action="probe"`): принудительный сброс DNS-кэша, живой DNS-резолвинг, опрос mDNS/Zeroconf с проверкой живых PTR/A-записей, ICMP Ping с точным замером RTT (в мс) и экспертная локализация сбоя связи.
  - Поддержка быстрого опроса матриц статусов (`action="states"`) всех устройств парка (online/offline).
  - Открыта **Фаза 3 (P2)** дорожной карты проекта.
  - Добавлены модульные тесты сигнатур и аргументов, а также интеграционные тесты для онлайн и офлайн сценариев.

---
## [1.7.0] - 2026-08-21

### Added
- **Глобальный справочник меток и пакетные операции над устройствами (Пункт 2.4 ROADMAP):**
  - Добавлен новый инструмент `manage_labels(action, label_id, name, color, host, port)` для полного CRUD-управления глобальным справочником меток/тегов парка устройств (`labels/*` API): просмотр каталога, создание с HEX-цветом `#rrggbb`, редактирование и каскадное удаление.
  - Добавлен новый инструмент `batch_manage_devices(action, configurations, label_ids, updates, host, port)` для высокопроизводительных пакетных операций над группой устройств (`devices/*_bulk` API): массовая архивация (`devices/archive_bulk`), массовое безвозвратное удаление (`devices/delete_bulk`) и массовое/индивидуальное назначение меток (`devices/set_labels_bulk`).
  - Полностью завершена **Фаза 2 (P1)** дорожной карты проекта.
  - Добавлены модульные тесты валидации аргументов и интеграционные тесты полного жизненного цикла меток и пакетных операций с Teardown Guard.

---
## [1.6.0] - 2026-08-21

### Added
- **Управление секретами, Wi-Fi и Serial-портами хоста (Пункт 2.3 ROADMAP):**
  - Добавлен новый инструмент `manage_secrets(action, key, value, ssid, psk, host, port)` для безопасной работы с `secrets.yaml` и Wi-Fi реквизитами (`config/*` API). Реализована политика Zero Secret Leakage: просмотр ключей без раскрытия значений, маскирование в логах (`***`).
  - Добавлен новый инструмент `get_host_info(action, host, port)` для получения версий бэкенда Device Builder, ESPHome Core (`config/version`) и списка физических USB-Serial адаптеров хоста (`config/serial_ports`).
  - Разработан механизм Teardown Guard со снимком `secrets.yaml` в памяти для гарантированного сохранения исходных секретов пользователя при тестировании.
  - Добавлены модульные тесты сигнатур и валидации параметров, а также интеграционные тесты для `manage_secrets` и `get_host_info`.

---
## [1.5.0] - 2026-08-21

### Added
- **Каталог и схема компонентов ESPHome (Пункт 2.2 ROADMAP):**
  - Добавлен новый инструмент `search_components(action, query, category, platform, component_id, limit, offset, host, port)` для исследования каталога из более 940 компонентов ESPHome (`components/*`).
  - Поддержка поиска компонентов по ключевым словам, категориям (`category`) и платформам с пагинацией (`limit`, `offset`).
  - Предоставление детального технического паспорта компонента (`action="get"`): зависимости (I2C/SPI/UART), ограничения шин (`bus_constraints`), предоставляемые сущности (`provides`), ссылки на документацию `esphome.io` и флаг `multi_conf`.
  - Предоставление справочника категорий (`action="categories"`, 42 категории) и справочника поддерживаемых режимов пинов (`action="pin_modes"`) для GPIO-расширителей (PCF8574, MCP23xxx, PCA9554 и др.).
  - Добавлены модульные тесты сигнатуры и валидации, а также интеграционные тесты поиска, детального паспорта, категорий и режимов пинов.

---
## [1.4.0] - 2026-08-21

### Added
- **Автоматическая миграция устаревшего YAML синтаксиса (Пункт 2.1 ROADMAP):**
  - Добавлен новый инструмент `migrate_device_config(configuration, content, apply, host, port)` для автоматического обнаружения устаревших директив и ключевых слов ESPHome (`editor/migrate_config`).
  - Поддержка безопасного режима предпросмотра (Dry Run по умолчанию) и автоматического применения дифференциального патча к конфигурации устройства (`apply=True`).
  - Реализована функция `apply_yaml_diff` для точного позиционного наложения патчей с сохранением структуры YAML.
  - Добавлены модульные тесты валидации аргументов и сквозной интеграционный тест жизненного цикла миграции (`test_migrate_device_config_content_and_lifecycle`).

---
## [1.3.0] - 2026-08-21

### Added
- **Расширение каталога плат и управления задачами сборки (Пункт 1.4 ROADMAP):**
  - Поддержка расширенных фильтров поиска плат в каталоге (`get_board_info`): фильтрация по чипу (`variant`: esp32c3, esp32s3 и др.), MCU (`mcu`), тегам (`tag`: poe, eth, display) и смещение для пагинации (`offset`).
  - Поддержка очистки завершенных и ошибочных задач компиляции из памяти сервера (`manage_build_jobs(action="clear")`, API `firmware/clear`).
  - Поддержка отмены отложенного обновления устройства (`manage_build_jobs(action="clear_queued")`, API `firmware/clear_queued_update`).
  - Добавлены unit и интеграционные тесты для валидации аргументов и фильтрации каталога плат.

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
