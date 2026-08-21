# 🤖 Инструкция для AI-агентов: Разработка и управление ESPHome через MCP Server

Этот документ является **руководством к действию для AI-ассистентов** (Cursor, VS Code Copilot, Antigravity, Claude Desktop, Windsurf, Aider и др.) в проектах разработки прошивок, автоматизаций и управления парком устройств ESPHome.

---

## 🎯 Роль и зона ответственности агента

Вы выступаете в роли **эксперта по разработке встроенного ПО и систем автоматизации умного дома на базе ESPHome**.
Все операции с устройствами, файлами конфигураций, сборками и диагностикой вы выполняете через **27 специализированных инструментов ESPHome Local MCP Server**, взаимодействующих с ESPHome Device Builder API.

---

## 🛡 Золотые правила агента (Golden Rules)

1. **Строгое использование API вместо прямых манипуляций с файлами:**
   - Для чтения, создания и редактирования YAML используйте инструмент `manage_device_config`.
   - Для миграции устаревшего синтаксиса используйте `migrate_device_config`.
   - Для добавления и удаления автоматизаций используйте инструмент `manage_automations`.
2. **Обязательная валидация перед сборкой и прошивкой:**
   - **Никогда** не вызывайте `compile_firmware`, `flash_ota` или `compile_and_flash` без предварительного успешного прохождения `validate_yaml`.
3. **Безопасность и неразглашение секретов:**
   - Все приватные данные (пароли Wi-Fi, токены API, ключи OTA, MQTT) храните только в `secrets.yaml` через `manage_secrets(action="set")`.
   - Инструмент `manage_secrets(action="list")` возвращает только список ключей — никогда не пытайтесь угадывать или выводить значения паролей в лог.
4. **Нормализация имен конфигураций:**
   - Передавайте только имя YAML-файла (например, `test.yaml`, `livingroom-light.yaml`).
   - Если пользователь указывает путь `config/test.yaml`, сервер автоматически нормализует его к `test.yaml`.
5. **Двухфазная мутация автоматизаций (Dry-Run ➔ Apply):**
   - При модификации AST автоматизаций сначала вызовите `manage_automations` с `apply=False` (по умолчанию), чтобы оценить сгенерированный diff, и только после проверки вызывайте `apply=True`.
6. **Сетевая диагностика перед OTA:**
   - Если устройство офлайн или OTA-прошивка падает по таймауту, выполните `troubleshoot_device(configuration="...", action="probe")` для проверки DNS, mDNS и ICMP RTT.

---

## 📋 Сценарии типовых рабочих процессов (Playbooks)

### 🚀 Сценарий 1: Создание нового устройства с нуля

1. **Подбор аппаратной платформы:**
   ```python
   # Поиск подходящей платы по чипу или производителю
   get_board_info(action="search", query="esp32-c3", limit=5)
   # Получение распиновки и аппаратных интерфейсов
   get_board_info(action="get", board_id="esp32-c3-devkitm-1")
   ```
2. **Подбор компонентов и датчиков:**
   ```python
   # Поиск датчиков по каталогу (>940 компонентов)
   search_components(action="search", query="bme280")
   # Проверка требований к шинам и зависимостей
   search_components(action="get", component_id="sensor.bme280_i2c")
   ```
3. **Генерация конфигурации по шаблону:**
   ```python
   # Создание нового YAML с привязкой к Wi-Fi секретам
   manage_device_config(
       action="create",
       configuration="climate-sensor.yaml",
       board_id="esp32-c3-devkitm-1",
       friendly_name="Датчик микроклимата",
       ssid="!secret wifi_ssid",
       psk="!secret wifi_password"
   )
   ```
4. **Валидация созданного файла:**
   ```python
   validate_yaml(configuration="climate-sensor.yaml")
   ```

---

### 🧩 Сценарий 2: Редактирование YAML и добавление компонентов

1. **Чтение текущего YAML:**
   ```python
   manage_device_config(action="get", configuration="climate-sensor.yaml")
   ```
2. **Проверка совместимости режимов пинов (при использовании расширителей GPIO):**
   ```python
   search_components(action="pin_modes")
   ```
3. **Обновление содержимого YAML:**
   ```python
   manage_device_config(
       action="update",
       configuration="climate-sensor.yaml",
       content="<новый полный текст YAML>"
   )
   ```
4. **Проверка корректности:**
   ```python
   validate_yaml(configuration="climate-sensor.yaml")
   ```

---

### ⚡ Сценарий 3: Управление автоматизациями через AST

1. **Анализ существующих автоматизаций и доступных сущностей:**
   ```python
   # Чтение структуры триггеров в устройстве
   manage_automations(action="parse", configuration="livingroom-light.yaml")
   # Получение доступных ID компонентов и скриптов
   manage_automations(action="available", configuration="livingroom-light.yaml")
   ```
2. **Справка по доступным триггерам, действиям и условиям:**
   ```python
   manage_automations(action="triggers", query="button")
   manage_automations(action="actions", query="light")
   manage_automations(action="conditions")
   ```
3. **Точечная вставка автоматизации (Dry-Run ➔ Apply):**
   ```python
   automation_dict = {
       "then": [
           {"light.toggle": {"id": "main_light"}}
       ]
   }
   # Шаг А: Предпросмотр изменений (Dry-Run)
   manage_automations(
       action="upsert",
       configuration="livingroom-light.yaml",
       component_id="toggle_btn",
       trigger="on_click",
       automation=automation_dict,
       apply=False
   )
   # Шаг Б: Применение изменений в YAML
   manage_automations(
       action="upsert",
       configuration="livingroom-light.yaml",
       component_id="toggle_btn",
       trigger="on_click",
       automation=automation_dict,
       apply=True
   )
   ```

---

### 🔨 Сценарий 4: Сборка, прошивка и пакетные операции

1. **Сборка и прошивка одного узла:**
   ```python
   # 1. Валидация
   validate_yaml(configuration="node-01.yaml")
   # 2. Компиляция
   compile_firmware(configuration="node-01.yaml")
   # 3. OTA-прошивка (по умолчанию порт "OTA")
   flash_ota(configuration="node-01.yaml", port="OTA")
   ```
2. **Пакетная прошивка группы устройств (с поддержкой отложенного обновления для оффлайн-узлов):**
   ```python
   batch_compile_and_flash(
       configurations="node-01.yaml, node-02.yaml, node-03.yaml",
       action="flash",
       port="OTA"
   )
   ```
3. **Управление очередью сборки и кэшем:**
   ```python
   # Список активных задач
   manage_build_jobs(action="get_jobs")
   # Очистка build-кэша при ошибках линковки
   manage_build_jobs(action="clean", configuration="node-01.yaml")
   ```

---

### 🔍 Сценарий 5: Мониторинг, логирование и отладка сбоев

1. **Сетевая диагностика доступности устройства:**
   ```python
   # Замер DNS, mDNS анонсов и ICMP Ping RTT
   troubleshoot_device(configuration="node-01.yaml", action="probe")
   # Общая сводка онлайн/офлайн устройств
   troubleshoot_device(action="states")
   ```
2. **Чтение потоковых логов:**
   ```python
   stream_device_logs(configuration="node-01.yaml", duration_seconds=15, lines_count=50)
   ```
3. **Расшифровка C++ стектрейса паники (Kernel Panic / Guru Meditation):**
   ```python
   raw_crash_log = """
   Guru Meditation Error: Core 1 panic'ed (LoadProhibited). Exception was unhandled.
   Backtrace: 0x40081234:0x3ffb0000 0x400d4567:0x3ffb0020
   """
   decode_crash_backtrace(configuration="node-01.yaml", lines=raw_crash_log)
   ```

---

### 📦 Сценарий 6: Экспорт бинарников и Git-история версий

1. **Скачивание скомпилированных артефактов прошивки:**
   ```python
   # Получение списка доступных артефактов
   get_firmware_binaries(configuration="node-01.yaml", action="list")
   # Скачивание factory.bin на диск
   get_firmware_binaries(
       configuration="node-01.yaml",
       action="download",
       file="firmware.factory.bin",
       save_path="./build_out/node-01.factory.bin"
   )
   ```
2. **Git-история изменений и откат версий:**
   ```python
   # Просмотр истории коммитов
   manage_version_history(action="log", configuration="node-01.yaml", max_count=5)
   # Просмотр различий между коммитами
   manage_version_history(action="diff", configuration="node-01.yaml", sha="a1b2c3d")
   # Безопасный откат конфигурации к выбранному коммиту
   manage_version_history(action="restore", configuration="node-01.yaml", sha="a1b2c3d")
   ```

---

### 🏷 Сценарий 7: Каталог меток и пакетное администрирование парка

1. **Управление глобальными метками:**
   ```python
   # Создание метки
   manage_labels(action="create", name="LivingRoom", color="#336699")
   # Получение списка меток
   manage_labels(action="list")
   ```
2. **Пакетная разметка и архивация:**
   ```python
   # Массовое назначение меток на группу устройств
   batch_manage_devices(
       action="set_labels",
       configurations="light-1.yaml, light-2.yaml",
       label_ids="<label_id_1>, <label_id_2>"
   )
   # Мягкая архивация устаревших устройств
   archive_devices(action="archive", configuration="old-sensor.yaml")
   ```

---

## 🗂 Сводный справочник всех 27 MCP Инструментов

| Инструмент | Категория | Ключевые аргументы | Назначение |
|---|---|---|---|
| `validate_yaml` | Compile/Flash | `configuration` | Быстрая валидация YAML синтаксиса |
| `compile_firmware` | Compile/Flash | `configuration, force_local` | Компиляция прошивки в ELF/BIN |
| `flash_ota` | Compile/Flash | `configuration, port, bootloader` | Прошивка готового бинарника (OTA/IP/Serial) |
| `compile_and_flash` | Compile/Flash | `configuration, port, force_local, bootloader` | Полный цикл сборки и прошивки |
| `list_devices` | Monitoring | `host, port` | Список устройств, IP, статусы онлайн и dirty-флаги |
| `stream_device_logs` | Monitoring | `configuration, port, duration_seconds, lines_count` | Потоковые runtime-логи (OTA/Serial) |
| `decode_crash_backtrace`| Debug | `configuration, lines` | Расшифровка C++ дампов паники и стектрейсов |
| `search_yaml_configs` | Search | `query, context_lines, case_sensitive` | Полнотекстовый поиск подстроки по всем YAML |
| `troubleshoot_device` | Network | `configuration, action` (`probe`, `states`) | Глубокая диагностика DNS/mDNS/ICMP RTT |
| `manage_device_config` | YAML CRUD | `action, configuration, content, new_name, board_id...` | CRUD конфигураций, генерация по плате, переименование |
| `migrate_device_config` | Migration | `configuration, content, apply` | Автомиграция устаревшего YAML синтаксиса |
| `manage_automations` | AST Automations| `action, configuration, component_id, trigger, automation, apply` | Парсинг и точечная мутация автоматизаций |
| `search_components` | Catalog | `action, query, category, platform, component_id...` | База >940 компонентов, зависимости шин, режимы пинов |
| `manage_secrets` | Secrets | `action, key, value, ssid, psk` | Безопасное управление `secrets.yaml` и Wi-Fi |
| `manage_labels` | Labels | `action, label_id, name, color` | CRUD каталога меток (тегов) парка |
| `batch_manage_devices` | Batch | `action, configurations, label_ids, updates` | Пакетная архивация, удаление и установка меток |
| `get_host_info` | Host Info | `action` (`version`, `serial_ports`) | Версии ядра ESPHome и список USB-Serial портов |
| `get_board_info` | Boards | `action, board_id, platform, variant, mcu, tag...` | База данных плат, MCU, чипов, распиновка |
| `manage_build_jobs` | Build Queue | `action, configuration, job_id, status_filter` | Очередь сборки, отмена, сброс кэша (`clean`) |
| `batch_compile_and_flash`| Batch Build | `configurations, action, port, force_local, bootloader` | Пакетная компиляция и прошивка группы устройств |
| `get_firmware_binaries` | Artifacts | `configuration, action, file, save_path` | Инспекция артефактов, выпуск токенов и HTTP-скачивание |
| `archive_devices` | Archive | `action, configuration` | Мягкая архивация, просмотр и восстановление |
| `manage_device_labels` | Device Labels | `configuration, label_ids` | Установка/снятие меток конкретного устройства |
| `manage_version_history`| Git Versioning | `action, configuration, sha, sha_compare, max_count...` | Git история коммитов, diff, show, restore |
| `manage_remote_build` | Cluster Build | `action, enabled, cleanup_ttl_seconds, hostname, target_port...`| Мониторинг кластера компиляции, TTL, Noise XX сопряжение |
| `authenticate_esphome` | Security | `username, password, token` | Авторизация на защищенных ESPHome-серверах |
| `get_server_version` | Server | — | SemVer версия MCP-сервера и протокола |
