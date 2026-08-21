from mcp.server.fastmcp import FastMCP
import asyncio
import websockets
import json
import sys
import logging
import re
import os
import subprocess
import uuid
from __version__ import __version__, __version_info__

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

# 1. ЗАЩИТА ПРОТОКОЛА: Перенаправляем все логи в stderr.
# Обычный print() использовать категорически запрещено!
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("esphome-mcp")

FALLBACK_HOST = "localhost"
FALLBACK_PORT = 6052

FALLBACK_SSL = None  # None = автоопределение по порту

def load_env_config() -> tuple[str, int, bool | None]:
    """
    Загружает IP/хост, порт и флаг SSL из файла .env в формате TOML.
    Возвращает кортеж (host, port, ssl).
    - ssl=True  — принудительно wss://
    - ssl=False — принудительно ws://
    - ssl=None  — автоопределение: wss:// если port == 443 или 8443
    При отсутствии .env или при ошибке парсинга используются настройки по умолчанию.
    """
    env_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        ".env"
    ]
    env_file = None
    for path in env_paths:
        if os.path.isfile(path):
            env_file = path
            break

    if not env_file:
        logger.info(f"Файл .env не найден, используются настройки по умолчанию ({FALLBACK_HOST}:{FALLBACK_PORT})")
        return FALLBACK_HOST, FALLBACK_PORT, FALLBACK_SSL

    if tomllib is None:
        logger.warning(f"Модуль tomllib/tomli недоступен. Используются настройки по умолчанию ({FALLBACK_HOST}:{FALLBACK_PORT})")
        return FALLBACK_HOST, FALLBACK_PORT, FALLBACK_SSL

    try:
        with open(env_file, "rb") as f:
            data = tomllib.load(f)

        if not isinstance(data, dict):
            raise ValueError("Содержимое TOML файла должно содержать ключевые пары")

        # Извлекаем хост из корня TOML или секции [api] / [esphome]
        host = (
            data.get("host")
            or data.get("ip")
            or data.get("api", {}).get("host")
            or data.get("api", {}).get("ip")
            or data.get("esphome", {}).get("host")
            or data.get("esphome", {}).get("ip")
            or FALLBACK_HOST
        )

        port_val = (
            data.get("port")
            or data.get("api", {}).get("port")
            or data.get("esphome", {}).get("port")
            or FALLBACK_PORT
        )

        try:
            port = int(port_val)
        except (ValueError, TypeError):
            logger.warning(f"Некорректное значение порта в .env ({port_val!r}), используется {FALLBACK_PORT}")
            port = FALLBACK_PORT

        host_str = str(host).strip()
        if not host_str:
            host_str = FALLBACK_HOST

        # Читаем явный параметр ssl (bool или None если не задан)
        ssl_val = (
            data.get("ssl")
            or data.get("api", {}).get("ssl")
            or data.get("esphome", {}).get("ssl")
        )
        if ssl_val is None:
            ssl = FALLBACK_SSL  # автоопределение по порту
        elif isinstance(ssl_val, bool):
            ssl = ssl_val
        else:
            # Если вдруг строка ("true"/"false") — попробуем разобрать
            ssl = str(ssl_val).strip().lower() in ("true", "1", "yes")

        logger.info(f"Загружены настройки из .env: host={host_str!r}, port={port}, ssl={ssl!r}")
        return host_str, port, ssl

    except Exception as e:
        logger.warning(f"Ошибка чтения/парсинга .env: {e}. Используются настройки по умолчанию ({FALLBACK_HOST}:{FALLBACK_PORT})")
        return FALLBACK_HOST, FALLBACK_PORT, FALLBACK_SSL

DEFAULT_HOST, DEFAULT_PORT, DEFAULT_SSL = load_env_config()

def get_ws_url(host: str | None = None, port: int | None = None, ssl: bool | None = None) -> str:
    """
    Формирует URL подключения к ESPHome WebSocket API.

    Протокол (ws:// / wss://) определяется следующим образом:
    1. Явный параметр ssl=True/False имеет наивысший приоритет.
    2. Если ssl=None — используется DEFAULT_SSL из .env.
    3. Если DEFAULT_SSL тоже None — автоопределение: wss:// для портов 443 и 8443.
    """
    h = host if host else DEFAULT_HOST
    p = port if port is not None else DEFAULT_PORT

    # Определяем, нужен ли TLS
    if ssl is not None:
        use_ssl = ssl
    elif DEFAULT_SSL is not None:
        use_ssl = DEFAULT_SSL
    else:
        # Автоопределение по порту
        use_ssl = p in (443, 8443)

    scheme = "wss" if use_ssl else "ws"
    return f"{scheme}://{h}:{p}/ws"

# Инициализация MCP сервера
mcp = FastMCP("esphome-device-builder")
mcp._version = __version__
logger.info(f"ESPHome Local MCP Server v{__version__} инициализирован")

def clean_ansi(text: str) -> str:
    text = re.sub(r'\\(?:033|x1b|e)', '\x1b', text)
    ansi_escape = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
    return ansi_escape.sub('', text)

def resolve_configuration(configuration: str) -> str:
    """
    Нормализует параметр configuration для передачи в ESPHome API.
    
    Возвращает имя конфигурации (config_name) для ESPHome API.
    Вся информация обрабатывается исключительно через ESPHome API без доступа к локальным файлам.
    
    Правила:
      - "config/foo.yaml" → "foo.yaml"  — обрезаем префикс config/
      - "foo.yaml"        → "foo.yaml"  — имя конфигурации в ESPHome API
    """
    # Убираем префикс config/
    if configuration.startswith("config/"):
        configuration = configuration[7:]

    return configuration


async def execute_ws_command(host: str, command_type: str, args: dict,
                             port: int | None = None) -> str:
    """
    Выполняет команду ESPHome WebSocket API.
    """
    url = get_ws_url(host, port)
    output_log = []
    request_id = "1"
    
    logger.info(f"Подключение к {url} для выполнения {command_type}...")
    
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            # Сначала читаем ServerInfoMessage
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            # Если команда валидации, формат простой
            if command_type == "devices/validate":
                await ws.send(json.dumps({
                    "command": command_type,
                    "message_id": request_id,
                    "args": args
                }))
                
                async for message in ws:
                    data = json.loads(message)
                    
                    if data.get("error_code") or data.get("type") == "error":
                        logger.error(f"Ошибка API ESPHome: {data}")
                        return f"Ошибка API ESPHome: {data.get('details', data)}"
                    
                    if data.get("message_id") == request_id:
                        if data.get("event") == "output":
                            # Для валидации data может быть как строкой, так и объектом (разные версии API)
                            line_data = data.get("data", "")
                            if isinstance(line_data, dict):
                                line = line_data.get("line", "").strip()
                            else:
                                line = str(line_data).strip()
                            if line:
                                output_log.append(clean_ansi(line))
                        elif data.get("event") == "result":
                            success = data.get("data", {}).get("success", False)
                            status = "УСПЕШНО" if success else "ОШИБКА"
                            
                            lines_to_return = 15 if success else 60
                            tail_logs = "\n".join(output_log[-lines_to_return:])
                            
                            logger.info(f"Команда {command_type} завершена. Статус: {status}")
                            return f"Статус выполнения ({command_type}): {status}\n\nЛог процесса:\n{tail_logs}"
            
            # Для остальных команд (compile, install и т.д.) процесс двухшаговый:
            else:
                await ws.send(json.dumps({
                    "command": command_type,
                    "message_id": request_id,
                    "args": args
                }))
                
                job_id = None
                while True:
                    res = await ws.recv()
                    data = json.loads(res)
                    if data.get("error_code") or data.get("type") == "error":
                        logger.error(f"Ошибка API ESPHome: {data}")
                        return f"Ошибка API ESPHome: {data.get('details', data)}"
                        
                    if data.get("message_id") == request_id and "result" in data:
                        if data["result"] is None:
                            continue
                        job_id = data["result"].get("job_id")
                        break
                        
                if not job_id:
                    return f"Не удалось получить job_id для команды {command_type}"
                
                logger.info(f"Получен job_id: {job_id}, подписываемся на логи...")
                
                follow_id = "2"
                await ws.send(json.dumps({
                    "command": "firmware/follow_job",
                    "message_id": follow_id,
                    "args": {"job_id": job_id}
                }))
                
                async for message in ws:
                    data = json.loads(message)
                    
                    if data.get("message_id") == follow_id:
                        event = data.get("event")
                        
                        if event == "output":
                            line_data = data.get("data", "")
                            if isinstance(line_data, dict):
                                line = line_data.get("line", "").strip()
                            else:
                                line = str(line_data).strip()
                            if line:
                                output_log.append(clean_ansi(line))
                                    
                        elif event == "result":
                            status_data = data.get("data", {})
                            if isinstance(status_data, dict):
                                status_str = status_data.get("status")
                                success = (status_str == "completed" or status_data.get("success") == True)
                                status_text = "УСПЕШНО" if success else f"ОШИБКА ({status_str})"
                            else:
                                success = True
                                status_text = "УСПЕШНО"
                                
                            lines_to_return = 35 if success else 100
                            tail_logs = "\n".join(output_log[-lines_to_return:])
                            
                            logger.info(f"Команда {command_type} завершена. Статус: {status_text}")
                            return f"Статус выполнения ({command_type}): {status_text}\n\nЛог процесса:\n{tail_logs}"
                            
    except Exception as e:
        error_msg = f"Критическая ошибка соединения с Device Builder API ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg

# ==========================================
# ИНСТРУМЕНТЫ ДЛЯ АГЕНТА (TOOLS)
# ==========================================

@mcp.tool()
async def validate_yaml(configuration: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    """
    Инструмент 1: Только валидация YAML конфигурации через ESPHome API.
    
    Параметр configuration принимает:
      - имя файла: "mcp-test.yaml"
      - относительный путь с префиксом: "config/mcp-test.yaml"
    """
    config_name = resolve_configuration(configuration)
    return await execute_ws_command(host, "devices/validate",
                                    {"configuration": config_name}, port=port)

@mcp.tool()
async def compile_firmware(
    configuration: str,
    force_local: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 2: Только компиляция прошивки без загрузки.
    
    Параметры:
      - configuration: имя файла ("mcp-test.yaml") или относительный путь ("config/mcp-test.yaml")
      - force_local: если True, принудительная локальная сборка без использования кэша/кластера сборки
      - host: хост сервера ESPHome (по умолчанию из .env или localhost)
      - port: сетевой порт WebSocket API ESPHome (по умолчанию из .env или 6052)
    """
    config_name = resolve_configuration(configuration)
    args: dict = {"configuration": config_name}
    if force_local:
        args["force_local"] = True
    return await execute_ws_command(host, "firmware/compile", args, port=port)

@mcp.tool()
async def flash_ota(
    configuration: str,
    port: str = "OTA",
    bootloader: bool = False,
    host: str = DEFAULT_HOST,
    api_port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 3: Прошивка готового скомпилированного бинарника (OTA / Serial / IP).
    
    Параметры:
      - configuration: имя файла ("mcp-test.yaml") или относительный путь ("config/mcp-test.yaml")
      - port: целевой порт/адрес прошивки устройства. По умолчанию "OTA" (по воздуху).
              Также поддерживается явный IP-адрес/hostname ("192.168.1.105") или serial-порт ("/dev/ttyUSB0", "COM3")
      - bootloader: если True, прошивает также образ bootloader
      - host: хост сервера ESPHome (по умолчанию из .env или localhost)
      - api_port: сетевой порт WebSocket API ESPHome (по умолчанию из .env или 6052)
    """
    config_name = resolve_configuration(configuration)
    target_port = "OTA" if not port else str(port)
    args: dict = {"configuration": config_name, "port": target_port}
    if bootloader:
        args["bootloader"] = True
    return await execute_ws_command(host, "firmware/upload", args, port=api_port)

@mcp.tool()
async def compile_and_flash(
    configuration: str,
    port: str = "OTA",
    force_local: bool = False,
    bootloader: bool = False,
    host: str = DEFAULT_HOST,
    api_port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 4: Полный цикл сборки и прошивки (Компиляция + Загрузка по OTA / Serial / IP).
    
    Параметры:
      - configuration: имя файла ("mcp-test.yaml") или относительный путь ("config/mcp-test.yaml")
      - port: целевой порт/адрес прошивки устройства. По умолчанию "OTA" (по воздуху).
              Также поддерживается явный IP-адрес/hostname ("192.168.1.105") или serial-порт ("/dev/ttyUSB0", "COM3")
      - force_local: если True, принудительная локальная компиляция без кэша
      - bootloader: если True, прошивает также загрузчик (bootloader)
      - host: хост сервера ESPHome (по умолчанию из .env или localhost)
      - api_port: сетевой порт WebSocket API ESPHome (по умолчанию из .env или 6052)
    """
    config_name = resolve_configuration(configuration)
    target_port = "OTA" if not port else str(port)
    args: dict = {"configuration": config_name, "port": target_port}
    if force_local:
        args["force_local"] = True
    if bootloader:
        args["bootloader"] = True
    return await execute_ws_command(host, "firmware/install", args, port=api_port)

# ==========================================
# ФАЗА P0: ИНСТРУМЕНТЫ МОНИТОРИНГА И ОТЛАДКИ
# ==========================================

@mcp.tool()
async def list_devices(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    """
    Инструмент 5 (P0): Получение полного списка всех устройств ESPHome и их статусов.
    
    Возвращает список устройств с их именем, файлом конфигурации, статусом (online/offline),
    IP-адресом, версией ESPHome, флагом незакомпилированных изменений (has_pending_changes) и метками.
    """
    url = get_ws_url(host, port)
    msg_id = "list_devices_1"
    
    logger.info(f"Запрос списка устройств с {url}...")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."
                
            await ws.send(json.dumps({
                "command": "devices/list",
                "message_id": msg_id,
                "args": {}
            }))
            
            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка получения списка устройств: {data.get('details', data)}"
                
                result = data.get("result", {})
                if isinstance(result, dict):
                    devices = result.get("configured", result.get("devices", []))
                elif isinstance(result, list):
                    devices = result
                else:
                    devices = []
                
                if not devices:
                    return "Список устройств пуст или устройства не найдены."
                
                output = ["### Список устройств ESPHome:\n"]
                for dev in devices:
                    if not isinstance(dev, dict):
                        continue
                    config = dev.get("configuration", "N/A")
                    name = dev.get("name", "N/A")
                    friendly = dev.get("friendly_name") or name
                    
                    runtime = dev.get("runtime_state", {}) or {}
                    state = runtime.get("state", dev.get("state", "unknown"))
                    ip_addrs = runtime.get("ip_addresses", [])
                    ip_str = ", ".join(ip_addrs) if ip_addrs else "не определен"
                    deployed_ver = runtime.get("deployed_version", "неизвестно")
                    
                    pending = dev.get("has_pending_changes")
                    pending_str = "Да" if pending is True else ("Нет" if pending is False else "неизвестно")
                    update_avail = dev.get("update_available")
                    update_str = "Доступно" if update_avail else "Нет"
                    
                    labels = dev.get("labels", [])
                    labels_str = ", ".join(labels) if labels else "нет"
                    
                    output.append(
                        f"- **{friendly}** (`{config}`):\n"
                        f"  - Имя/Hostname: `{name}`\n"
                        f"  - Статус: **{state.upper()}** (IP: {ip_str})\n"
                        f"  - Версия прошивки: {deployed_ver}\n"
                        f"  - Изменения не прошиты: {pending_str}\n"
                        f"  - Обновление ESPHome: {update_str}\n"
                        f"  - Метки: {labels_str}"
                    )
                return "\n".join(output)
                
    except Exception as e:
        error_msg = f"Ошибка подключения при вызове list_devices ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def stream_device_logs(configuration: str, port: str = "OTA", duration_seconds: int = 10,
                               lines_count: int = 50, host: str = DEFAULT_HOST, api_port: int = DEFAULT_PORT) -> str:
    """
    Инструмент 6 (P0): Чтение и вывод логов работы устройства в реальном времени.
    
    Параметры:
      - configuration: имя конфигурации устройства на сервере ESPHome API
      - port: "OTA" (по умолчанию) или serial-порт (/dev/ttyUSB0, COM3 и т.д.)
      - duration_seconds: продолжительность сбора логов в секундах (1-60, по умолчанию 10)
      - lines_count: максимальное количество собираемых строк (по умолчанию 50)
    """
    config_name = resolve_configuration(configuration)
    duration_seconds = max(1, min(60, duration_seconds))
    lines_count = max(1, min(500, lines_count))
    
    url = get_ws_url(host, api_port)
    msg_id = "logs_stream_1"
    output_log = []
    
    logger.info(f"Подключение к {url} для чтения логов {config_name} (порт: {port}, время: {duration_seconds}s)...")
    
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."
            
            await ws.send(json.dumps({
                "command": "devices/logs",
                "message_id": msg_id,
                "args": {"configuration": config_name, "port": port}
            }))
            
            start_time = asyncio.get_event_loop().time()
            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                remaining_time = duration_seconds - elapsed
                if remaining_time <= 0 or len(output_log) >= lines_count:
                    break
                    
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining_time))
                except asyncio.TimeoutError:
                    break
                    
                data = json.loads(message)
                if data.get("error_code") or data.get("type") == "error":
                    return f"Ошибка чтения логов: {data.get('details', data)}"
                    
                if data.get("message_id") == msg_id and data.get("event") == "output":
                    line_data = data.get("data", "")
                    if isinstance(line_data, dict):
                        line = line_data.get("line", "").strip()
                    else:
                        line = str(line_data).strip()
                    if line:
                        output_log.append(clean_ansi(line))
                        
            if not output_log:
                return f"Логи устройства ({config_name}) не получены за {duration_seconds} сек. (Устройство оффлайн или тишина в порте)."
                
            tail = "\n".join(output_log[-lines_count:])
            return f"Собрано {len(output_log)} строк лога ({config_name}, {port}):\n\n{tail}"
                
    except Exception as e:
        error_msg = f"Ошибка чтения логов ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def decode_crash_backtrace(configuration: str, lines: list[str], host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    """
    Инструмент 7 (P0): Расшифровка C++ дампов паники/стектрейсов устройства (Backtrace decoder).
    
    Параметры:
      - configuration: имя конфигурации устройства на сервере ESPHome API
      - lines: массив строк лога, содержащих дампы паники (например ["Backtrace: 0x400d1234:0x3ffb1234 ..."])
    """
    config_name = resolve_configuration(configuration)
    url = get_ws_url(host, port)
    msg_id = "decode_bt_1"
    
    logger.info(f"Отправка дампа стека {config_name} для расшифровки...")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."
                
            await ws.send(json.dumps({
                "command": "devices/decode_backtrace",
                "message_id": msg_id,
                "args": {"configuration": config_name, "lines": lines}
            }))
            
            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка расшифровки стектрейса: {data.get('details', data)}"
                    
                res = data.get("result", {})
                decoded_items = res.get("decoded", [])
                stale = res.get("stale_build", False)
                unavail_reason = res.get("unavailable_reason")
                
                output = [f"### Результат расшифровки дампа паники ({config_name}):\n"]
                if unavail_reason:
                    output.append(f"⚠️ **Предупреждение:** Расшифровка частично или полностью недоступна ({unavail_reason}).")
                    if unavail_reason == "no_build":
                        output.append("Причина: Бинарник ещё не компилировался локально на этом сервере (отсутствуют symbols/ELF).")
                    elif unavail_reason == "no_backtrace":
                        output.append("Причина: В переданных строках не обнаружено адресов стека (0x...).")
                    output.append("")
                    
                if stale:
                    output.append("⚠️ **Внимание:** Конфигурация менялась с момента последней сборки (`stale_build=True`), символы могут быть неточными.\n")
                    
                if decoded_items:
                    output.append("Расшифрованные вызовы:")
                    for item in decoded_items:
                        idx = item.get("index", "")
                        text = item.get("text", "")
                        output.append(f"  [{idx}] {text}")
                else:
                    output.append("Расшифрованных адресов не найдено.")
                    
                return "\n".join(output)
                    
    except Exception as e:
        error_msg = f"Ошибка расшифровки стектрейса ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def search_yaml_configs(query: str, context_lines: int = 2, case_sensitive: bool = False, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    """
    Инструмент 8 (P0): Поиск подстроки по всем YAML-конфигурациям ESPHome.
    
    Параметры:
      - query: поисковая подстрока (например "sensor", "GPIO4", "i2c", "wifi")
      - context_lines: количество контекстных строк до и после совпадения (0-10, по умолчанию 2)
      - case_sensitive: учитывать ли регистр символов (по умолчанию False)
    """
    url = get_ws_url(host, port)
    msg_id = "yaml_search_1"
    
    logger.info(f"Поиск подстроки {query!r} в YAML-конфигурациях ESPHome...")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."
                
            await ws.send(json.dumps({
                "command": "yaml/search",
                "message_id": msg_id,
                "args": {
                    "query": query,
                    "context_lines": max(0, min(10, context_lines)),
                    "case_sensitive": case_sensitive
                }
            }))
            
            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка поиска по YAML: {data.get('details', data)}"
                    
                results = data.get("result", [])
                if not results:
                    return f"Совпадений по запросу {query!r} не найдено."
                    
                output = [f"### Результаты поиска по запросу {query!r}:\n"]
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    config = item.get("configuration", "N/A")
                    friendly = item.get("friendly_name") or item.get("device_name") or config
                    matches = item.get("matches", [])
                    total = item.get("total_matches", len(matches))
                    
                    output.append(f"📄 **{friendly}** (`{config}`) — всего совпадений: {total}")
                    for m in matches:
                        line_num = m.get("line_number")
                        line_text = m.get("line_text", "").strip()
                        before = m.get("before", [])
                        after = m.get("after", [])
                        
                        output.append(f"  - **Строка {line_num}:** `{line_text}`")
                        if before:
                            for b in before:
                                output.append(f"      `{b.strip()}`")
                        if after:
                            for a in after:
                                output.append(f"      `{a.strip()}`")
                    output.append("")
                    
                return "\n".join(output)
                
    except Exception as e:
        error_msg = f"Ошибка поиска по YAML ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


# ==========================================
# ФАЗА P1: УПРАВЛЕНИЕ КОНФИГУРАЦИЯМИ, ПЛАТАМИ И СБОРКАМИ
# ==========================================

@mcp.tool()
async def manage_device_config(
    action: str,
    configuration: str,
    content: str = "",
    new_name: str = "",
    board_id: str = "",
    friendly_name: str = "",
    ssid: str = "",
    psk: str = "",
    config_only: bool = True,
    overwrite: bool = True,
    allow_wipe: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 9 (P1): Управление конфигурацией устройств ESPHome через API.

    Параметр action поддерживает следующие действия:
      - "get"    — прочитать содержимое YAML-конфигурации устройства
      - "update" — записать новое содержимое YAML-конфигурации (требует content)
      - "create" — создать новую конфигурацию устройства:
                   - по готовому YAML (параметр content)
                   - или по шаблону платы (параметр board_id, опционально friendly_name, ssid, psk)
      - "rename" — переименовать конфигурацию (требует new_name):
                   - config_only=True (по умолчанию) — офлайн переименование файлов на диске
                   - config_only=False — онлайн двухшаговый процесс (сборка -> OTA-прошивка -> переименование)
      - "delete" — удалить конфигурацию и связанные файлы

    Параметры:
      - configuration: имя файла ("test.yaml") или относительный путь ("config/test.yaml")
      - content: содержимое YAML для действий "update" или "create"
      - new_name: новое имя для действия "rename"
      - board_id: ID платы для создания по шаблону (например "esp32dev", "d1_mini", "nodemcuv2")
      - friendly_name: понятное имя устройства (например "Датчик климата")
      - ssid: имя Wi-Fi сети (сохраняется в secrets.yaml с использованием !secret)
      - psk: пароль Wi-Fi сети
      - config_only: переименование только файлов конфигурации (True) или с онлайн OTA-прошивкой (False)
      - overwrite: перезаписывать ли существующий файл при создании
      - allow_wipe: разрешить перезапись при действии "update"
      - host: хост сервера ESPHome
      - port: сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "manage_cfg_1"

    command_map = {
        "get":    "devices/get_config",
        "update": "devices/update_config",
        "create": "devices/create",
        "rename": "devices/rename",
        "delete": "devices/delete",
    }

    action = action.strip().lower()
    if action not in command_map:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: get, update, create, rename, delete."

    command = command_map[action]
    config_name = resolve_configuration(configuration)

    # Формируем аргументы по действию
    if action == "get":
        args = {"configuration": config_name}
    elif action == "update":
        if not content:
            return "Ошибка: для действия 'update' необходимо указать параметр content."
        args = {"configuration": config_name, "content": content, "allow_wipe": allow_wipe}
    elif action == "create":
        name = config_name.removesuffix(".yaml")
        args = {"name": name, "overwrite": overwrite}
        if content:
            args["file_content"] = content
        elif board_id:
            args["board_id"] = board_id
            if friendly_name:
                args["friendly_name"] = friendly_name
            if ssid:
                args["ssid"] = ssid
            if psk:
                args["psk"] = psk
        else:
            return (
                "Ошибка: для действия 'create' необходимо указать либо параметр content "
                "(содержимое YAML), либо board_id (ID платы для генерации из шаблона)."
            )
    elif action == "rename":
        if not new_name:
            return "Ошибка: для действия 'rename' необходимо указать параметр new_name."
        new_name_clean = resolve_configuration(new_name).removesuffix(".yaml")
        args = {
            "configuration": config_name,
            "new_name": new_name_clean,
            "config_only": config_only
        }
    elif action == "delete":
        args = {"configuration": config_name}

    logger.info(f"manage_device_config: action={action!r}, configuration={config_name!r}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            await ws.send(json.dumps({
                "command": command,
                "message_id": msg_id,
                "args": args
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка команды {action!r}: {data.get('details', data)}"

                result = data.get("result")

                if action == "get":
                    if not result:
                        return f"Файл {config_name!r} пустой или не найден."
                    return f"### Содержимое конфигурации `{config_name}`:\n\n```yaml\n{result}\n```"

                elif action == "update":
                    return f"✅ Конфигурация `{config_name}` успешно обновлена."

                elif action == "create":
                    real_name = (result or {}).get("configuration", f"{args['name']}.yaml")
                    return f"✅ Конфигурация создана: `{real_name}`"

                elif action == "rename":
                    # Проверяем, запущена ли двухшаговая задача прошивки (при config_only=False)
                    job_id = (result or {}).get("job_id") if isinstance(result, dict) else None
                    if job_id and not config_only:
                        logger.info(f"Онлайн-переименование запустило задачу {job_id}, подписываемся на логи...")
                        follow_id = "rename_follow_2"
                        await ws.send(json.dumps({
                            "command": "firmware/follow_job",
                            "message_id": follow_id,
                            "args": {"job_id": job_id}
                        }))

                        output_log = []
                        async for follow_msg in ws:
                            f_data = json.loads(follow_msg)
                            if f_data.get("message_id") != follow_id:
                                continue
                            f_event = f_data.get("event")
                            if f_event == "output":
                                line_data = f_data.get("data", "")
                                if isinstance(line_data, dict):
                                    line = line_data.get("line", "").strip()
                                else:
                                    line = str(line_data).strip()
                                if line:
                                    output_log.append(clean_ansi(line))
                            elif f_event == "result":
                                status_data = f_data.get("data", {})
                                success = (status_data.get("status") == "completed" or status_data.get("success") is True) if isinstance(status_data, dict) else True
                                status_text = "УСПЕШНО" if success else "ОШИБКА"
                                tail_logs = "\n".join(output_log[-35:])
                                return f"✅ Онлайн-переименование `{config_name}` → `{new_name_clean}.yaml` завершено ({status_text}).\n\nЛог процесса:\n{tail_logs}"

                    new_cfg = (result or {}).get("configuration", f"{new_name_clean}.yaml") if isinstance(result, dict) else f"{new_name_clean}.yaml"
                    return f"✅ Конфигурация `{config_name}` успешно переименована в `{new_cfg}`."

                elif action == "delete":
                    return f"✅ Конфигурация `{config_name}` и связанные файлы удалены."

    except Exception as e:
        error_msg = f"Критическая ошибка manage_device_config ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


def apply_yaml_diff(original: str, diff: dict) -> str:
    """Применяет дифференциальный патч ESPHome API (1-indexed fromLine/toLine) к тексту YAML."""
    if not diff or not isinstance(diff, dict):
        return original
    from_line = diff.get("fromLine", 1)
    to_line = diff.get("toLine", 1)
    replacement = diff.get("replacement", "")

    lines = original.splitlines(keepends=True)
    prefix = lines[:max(0, from_line - 1)]
    suffix = lines[to_line:]

    if replacement and not replacement.endswith("\n") and (suffix or original.endswith("\n")):
        replacement += "\n"

    migrated_lines = prefix + [replacement] + suffix
    return "".join(migrated_lines)


@mcp.tool()
async def migrate_device_config(
    configuration: str = "",
    content: str = "",
    apply: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 16 (P1): Автоматическая миграция устаревшего YAML синтаксиса ESPHome.

    Анализирует YAML на наличие устаревших директив и ключевых слов (services -> actions,
    clk_mode -> clk, rgb_order -> channel_colors, rp2040: -> rp2: и др.), получает дифференциальный
    патч через API (editor/migrate_config) и опционально применяет его к файлу устройства.

    Параметры:
      - configuration:  имя YAML-файла устройства (например "test.yaml"). Если указан,
                        исходный YAML считывается с сервера автоматически.
      - content:        прямой текст YAML-конфигурации (для проверки без привязки к файлу).
      - apply:          если True и указан configuration, автоматически сохраняет мигрированный
                        YAML в файл устройства через API. Если False (по умолчанию) — выполняет
                        безопасный предпросмотр (Dry Run).
      - host:           хост сервера ESPHome
      - port:           сетевой порт WebSocket API ESPHome
    """
    if not configuration and not content:
        return "Ошибка: необходимо указать либо параметр 'configuration' (имя файла устройства), либо 'content' (текст YAML)."

    url = get_ws_url(host, port)
    msg_id = "migrate_config_1"
    config_name = resolve_configuration(configuration) if configuration else ""

    logger.info(f"migrate_device_config: configuration={config_name!r}, apply={apply!r}, content_len={len(content)}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            target_content = content
            # Если content не передан, запрашиваем актуальный текст конфига с сервера
            if not target_content and config_name:
                get_id = "migrate_get_cfg_1"
                await ws.send(json.dumps({
                    "command": "devices/get_config",
                    "message_id": get_id,
                    "args": {"configuration": config_name}
                }))
                async for message in ws:
                    data = json.loads(message)
                    if data.get("message_id") != get_id:
                        continue
                    if data.get("error_code"):
                        return f"Ошибка чтения конфигурации `{config_name}`: {data.get('details', data)}"
                    result = data.get("result", {})
                    target_content = result.get("content", "") if isinstance(result, dict) else str(result)
                    break

            if not target_content:
                return f"Ошибка: не удалось получить содержимое конфигурации `{config_name}`."

            # Отправка команды миграции
            await ws.send(json.dumps({
                "command": "editor/migrate_config",
                "message_id": msg_id,
                "args": {"content": target_content}
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка команды editor/migrate_config: {data.get('details', data)}"

                result = data.get("result", {})
                yaml_diff = result.get("yaml_diff") if isinstance(result, dict) else None
                changes = result.get("changes", []) if isinstance(result, dict) else []

                if not yaml_diff and not changes:
                    target_label = f"устройства `{config_name}`" if config_name else "переданного YAML"
                    return f"✅ Конфигурация {target_label} не требует миграции — синтаксис полностью актуален."

                migrated_content = apply_yaml_diff(target_content, yaml_diff)

                output = ["### 🔄 Анализ миграции синтаксиса ESPHome:\n"]
                if changes:
                    output.append("**Обнаруженные устаревшие директивы:**")
                    for ch in changes:
                        scope = ch.get("scope", "")
                        old_field = ch.get("old", "")
                        new_field = ch.get("new", "")
                        since = ch.get("since", "")
                        scope_str = f" `{scope}.{old_field}`" if scope else f" `{old_field}`"
                        since_str = f" (устарело с версии {since})" if since else ""
                        output.append(f"- {scope_str} ➔ `{new_field}`{since_str}")

                if yaml_diff:
                    from_l = yaml_diff.get("fromLine")
                    to_l = yaml_diff.get("toLine")
                    output.append(f"\n**Дифференциальный патч (строки {from_l}–{to_l}):**")
                    output.append("```yaml")
                    output.append(yaml_diff.get("replacement", "").strip())
                    output.append("```")

                if apply and config_name:
                    update_id = "migrate_update_cfg_1"
                    await ws.send(json.dumps({
                        "command": "devices/update_config",
                        "message_id": update_id,
                        "args": {
                            "configuration": config_name,
                            "content": migrated_content
                        }
                    }))
                    async for u_msg in ws:
                        u_data = json.loads(u_msg)
                        if u_data.get("message_id") != update_id:
                            continue
                        if u_data.get("error_code"):
                            return f"Ошибка применения миграции к `{config_name}`: {u_data.get('details', u_data)}"
                        output.append(f"\n✅ **Миграция успешно применена и сохранена в `{config_name}`.**")
                        return "\n".join(output)

                if not apply and config_name:
                    output.append(f"\n💡 *Для применения и сохранения изменений запустите команду с параметром `apply=True`.*")

                output.append("\n**Итоговый мигрированный YAML:**")
                output.append("```yaml")
                output.append(migrated_content.strip())
                output.append("```")

                return "\n".join(output)

    except Exception as e:
        error_msg = f"Критическая ошибка migrate_device_config ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def search_components(
    action: str = "search",
    query: str = "",
    category: str = "",
    platform: str = "",
    component_id: str = "",
    limit: int = 20,
    offset: int = 0,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 18 (P1): Поиск и исследование каталога компонентов ESPHome (>940 компонентов).

    Позволяет агенту находить компоненты, датчики, дисплеи и приводы, узнавать их зависимости
    (i2c, spi, uart), ограничения шин, ссылки на документацию и поддерживаемые режимы GPIO.

    Параметр action поддерживает:
      - "search"     — поиск компонентов по query, категории, платформе с пагинацией
      - "get"        — подробная информация о конкретном компоненте (требует component_id или query)
      - "categories" — список всех категорий компонентов ESPHome с количеством записей
      - "pin_modes"  — справочник поддерживаемых режимов пинов для GPIO-расширителей

    Параметры:
      - query:        строка поиска (название, чип, например "bme280", "dht", "ssd1306")
      - category:     фильтр по категории (например "sensor", "display", "light", "climate")
      - platform:     фильтр по платформе чипа (например "esp32", "rp2040")
      - component_id: точный ID компонента для action="get" (например "sensor.bme280_i2c")
      - limit:        максимальное количество результатов (по умолчанию 20)
      - offset:       смещение для пагинации (по умолчанию 0)
      - host:         хост сервера ESPHome
      - port:         сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "comp_tool_1"
    action = action.strip().lower()

    valid_actions = ("search", "get", "categories", "pin_modes")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: {', '.join(valid_actions)}."

    if action == "get" and not component_id and not query:
        return "Ошибка: для действия 'get' необходимо указать параметр 'component_id' или 'query'."

    if action == "search":
        command = "components/get_components"
        args: dict = {"limit": limit}
        if offset > 0:
            args["offset"] = offset
        if query:
            args["query"] = query
        if category:
            args["category"] = category
        if platform:
            args["platform"] = platform

    elif action == "get":
        target_q = component_id or query
        command = "components/get_components"
        args = {"query": target_q, "limit": 10}

    elif action == "categories":
        command = "components/get_categories"
        args = {}

    elif action == "pin_modes":
        command = "components/get_pin_registry_modes"
        args = {}

    logger.info(f"search_components: action={action!r}, query={query!r}, category={category!r}, component_id={component_id!r}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            await ws.send(json.dumps({
                "command": command,
                "message_id": msg_id,
                "args": args
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка команды components ({action!r}): {data.get('details', data)}"

                result = data.get("result", {})

                if action == "search":
                    comps = result.get("components", []) if isinstance(result, dict) else []
                    total = result.get("total", len(comps)) if isinstance(result, dict) else len(comps)
                    if not comps:
                        return "Компоненты по заданным критериям не найдены."

                    output = [f"### Каталог компонентов ESPHome (найдено: {total}, показано: {len(comps)}):\n"]
                    for c in comps:
                        cid = c.get("id", "N/A")
                        cname = c.get("name", cid)
                        cat = c.get("category", "")
                        desc = c.get("description", "").strip()
                        deps = ", ".join(c.get("dependencies", []))
                        deps_str = f" | Зависимости: `{deps}`" if deps else ""
                        docs = c.get("docs_url", "")
                        docs_str = f" | [Документация]({docs})" if docs else ""
                        output.append(f"- **{cname}** (`{cid}`) [{cat}]{deps_str}{docs_str}")
                        if desc:
                            short_desc = desc[:140] + ("..." if len(desc) > 140 else "")
                            output.append(f"  *{short_desc}*")
                    return "\n".join(output)

                elif action == "get":
                    target_q = component_id or query
                    comps = result.get("components", []) if isinstance(result, dict) else []
                    matched = None
                    # Ищем точное совпадение по ID, либо первый результат
                    for c in comps:
                        if c.get("id") == target_q or c.get("id") == f"sensor.{target_q}" or c.get("name", "").lower() == target_q.lower():
                            matched = c
                            break
                    if not matched and comps:
                        matched = comps[0]

                    if not matched:
                        return f"Компонент `{target_q}` не найден в каталоге ESPHome."

                    cid = matched.get("id", "N/A")
                    cname = matched.get("name", cid)
                    cat = matched.get("category", "?")
                    desc = matched.get("description", "")
                    docs = matched.get("docs_url", "")
                    deps = matched.get("dependencies", []) or []
                    bus_c = matched.get("bus_constraints", {}) or {}
                    platforms = matched.get("supported_platforms", []) or []
                    provides = matched.get("provides", []) or []
                    multi_conf = matched.get("multi_conf", True)

                    output = [f"### Компонент ESPHome: **{cname}** (`{cid}`)\n"]
                    output.append(f"- **Категория:** `{cat}`")
                    output.append(f"- **Несколько экземпляров (multi_conf):** {'Да' if multi_conf else 'Нет'}")
                    if desc:
                        output.append(f"- **Описание:** {desc}")
                    if docs:
                        output.append(f"- **Документация:** {docs}")
                    if deps:
                        output.append(f"- **Зависимости (подсистемы/шины):** {', '.join(f'`{d}`' for d in deps)}")
                    if platforms:
                        output.append(f"- **Поддерживаемые платформы:** {', '.join(f'`{p}`' for p in platforms)}")
                    if provides:
                        output.append(f"- **Предоставляет сущности:** {', '.join(f'`{pr}`' for pr in provides)}")

                    if bus_c:
                        output.append("\n**Ограничения шин связи (bus constraints):**")
                        for bus_name, constraints in bus_c.items():
                            c_items = []
                            if isinstance(constraints, dict):
                                for k, v in constraints.items():
                                    c_items.append(f"{k}={v}")
                            c_str = f" ({', '.join(c_items)})" if c_items else ""
                            output.append(f"  - `{bus_name}`{c_str}")

                    return "\n".join(output)

                elif action == "categories":
                    cats = result if isinstance(result, list) else []
                    if not cats:
                        return "Категории компонентов не найдены."
                    output = [f"### Категории компонентов ESPHome ({len(cats)} шт.):\n"]
                    for cat in cats:
                        cat_id = cat.get("id", "?")
                        cat_name = cat.get("name", cat_id)
                        count = cat.get("count", 0)
                        output.append(f"- `{cat_id}` — **{cat_name}** ({count} компонентов)")
                    return "\n".join(output)

                elif action == "pin_modes":
                    pins_dict = result if isinstance(result, dict) else {}
                    if not pins_dict:
                        return "Справочник режимов пинов пуст."
                    output = [f"### Справочник режимов пинов для GPIO-расширителей ({len(pins_dict)} микросхем):\n"]
                    for chip, modes in pins_dict.items():
                        modes_str = ", ".join(f"`{m}`" for m in modes) if isinstance(modes, list) else str(modes)
                        output.append(f"- **{chip}**: {modes_str}")
                    return "\n".join(output)

    except Exception as e:
        error_msg = f"Критическая ошибка search_components ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def manage_secrets(
    action: str = "list",
    key: str = "",
    value: str = "",
    ssid: str = "",
    psk: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 19 (P1): Безопасное управление секретами и реквизитами Wi-Fi (secrets.yaml).

    Позволяет агенту просматривать список существующих ключей секретов (без раскрытия
    приватных значений), атомарно добавлять/обновлять секреты и настраивать Wi-Fi реквизиты.

    Параметр action поддерживает:
      - "list"     — просмотр списка ключей существующих секретов (значения скрыты)
      - "set"      — запись/обновление секрета по ключу (key) и значению (value)
      - "set_wifi" — быстрая настройка общих реквизитов Wi-Fi (ssid, psk)

    Политика безопасности:
      - Значения секретов (пароли, токены) НИКОГДА не возвращаются в ответах инструмента
        и не выводятся в логи MCP-сервера.
      - В логах параметры маскируются: value='***', psk='***'.

    Параметры:
      - key:   имя ключа секрета (для action="set", например "mqtt_broker_password")
      - value: значение секрета (для action="set")
      - ssid:  имя сети Wi-Fi (для action="set_wifi")
      - psk:   пароль сети Wi-Fi (для action="set_wifi")
      - host:  хост сервера ESPHome
      - port:  сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "secrets_tool_1"
    action = action.strip().lower()

    valid_actions = ("list", "get", "set", "set_wifi")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: list, set, set_wifi."

    if action in ("list", "get"):
        command = "config/get_secrets"
        args = {}
    elif action == "set":
        if not key or not value:
            return "Ошибка: для действия 'set' необходимо указать оба параметра: 'key' (имя ключа) и 'value' (значение)."
        command = "config/set_secret"
        args = {"key": key, "value": value}
    elif action == "set_wifi":
        if not ssid:
            return "Ошибка: для действия 'set_wifi' необходимо указать параметр 'ssid' (имя сети Wi-Fi)."
        command = "config/set_wifi_credentials"
        args = {"ssid": ssid, "psk": psk}

    # Безопасное логирование без раскрытия паролей/токенов
    log_value = "***" if value else ""
    log_psk = "***" if psk else ""
    logger.info(f"manage_secrets: action={action!r}, key={key!r}, value={log_value!r}, ssid={ssid!r}, psk={log_psk!r}")

    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            await ws.send(json.dumps({
                "command": command,
                "message_id": msg_id,
                "args": args
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка команды secrets ({action!r}): {data.get('details', data)}"

                result = data.get("result", {})

                if action in ("list", "get"):
                    keys = result if isinstance(result, list) else []
                    if not keys:
                        return "Файл secrets.yaml пуст или секреты не зарегистрированы."
                    output = [f"### Зарегистрированные ключи секретов ESPHome ({len(keys)} шт.):\n"]
                    output.append("*(В целях безопасности приватные значения секретов не отображаются)*\n")
                    for k in sorted(keys):
                        output.append(f"- `{k}`")
                    output.append(f"\n💡 *Использование в YAML:* `!secret <key_name>`")
                    return "\n".join(output)

                elif action == "set":
                    created = result.get("created", False) if isinstance(result, dict) else False
                    status_text = "создан" if created else "обновлен"
                    return f"✅ Секрет `{key}` успешно {status_text} в `secrets.yaml` (значение скрыто в целях безопасности)."

                elif action == "set_wifi":
                    return f"✅ Реквизиты сети Wi-Fi (`ssid`: `{ssid}`) успешно записаны в `secrets.yaml` (ключи `wifi_ssid` и `wifi_password`, пароль скрыт)."

    except Exception as e:
        error_msg = f"Критическая ошибка manage_secrets ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def get_host_info(
    action: str = "version",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 20 (P1): Информация о хосте ESPHome, версиях и подключенных USB-Serial портах.

    Параметр action поддерживает:
      - "version"      — получение версии бэкенда Device Builder и ESPHome Core
      - "serial_ports" — список обнаруженных физических USB-Serial адаптеров на хосте
      - "all"          — полная сводка (версии + список Serial-портов)

    Параметры:
      - host: хост сервера ESPHome
      - port: сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    action = action.strip().lower()

    valid_actions = ("version", "serial_ports", "ports", "all")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: version, serial_ports, all."

    logger.info(f"get_host_info: action={action!r}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            ver_data = {}
            ports_data = []

            # Запрашиваем version при необходимости
            if action in ("version", "all"):
                v_id = "host_ver_1"
                await ws.send(json.dumps({
                    "command": "config/version",
                    "message_id": v_id,
                    "args": {}
                }))
                async for msg in ws:
                    d = json.loads(msg)
                    if d.get("message_id") == v_id:
                        ver_data = d.get("result", {})
                        break

            # Запрашиваем serial_ports при необходимости
            if action in ("serial_ports", "ports", "all"):
                p_id = "host_ports_1"
                await ws.send(json.dumps({
                    "command": "config/serial_ports",
                    "message_id": p_id,
                    "args": {}
                }))
                async for msg in ws:
                    d = json.loads(msg)
                    if d.get("message_id") == p_id:
                        ports_data = d.get("result", [])
                        break

            output = []
            if action in ("version", "all"):
                srv_ver = ver_data.get("server_version", "N/A")
                core_ver = ver_data.get("esphome_version", "N/A")
                output.append("### Информация о сервере ESPHome:\n")
                output.append(f"- **ESPHome Core Version:** `{core_ver}`")
                output.append(f"- **Device Builder Backend:** `{srv_ver}`")

            if action in ("serial_ports", "ports", "all"):
                if output:
                    output.append("\n---\n")
                output.append(f"### Подключенные USB-Serial порты хоста ({len(ports_data)} шт.):\n")
                if not ports_data:
                    output.append("Физические USB-Serial адаптеры не обнаружены на хосте.")
                else:
                    for p in ports_data:
                        port_name = p.get("port", "N/A")
                        desc = p.get("description", "")
                        hwid = p.get("hwid", "")
                        output.append(f"- **`{port_name}`**: {desc}")
                        if hwid:
                            output.append(f"  *HWID:* `{hwid}`")

            return "\n".join(output)

    except Exception as e:
        error_msg = f"Критическая ошибка get_host_info ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def get_board_info(
    action: str = "list",
    board_id: str = "",
    platform: str = "",
    variant: str = "",
    mcu: str = "",
    tag: str = "",
    query: str = "",
    limit: int = 20,
    offset: int = 0,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 10 (P1): Получение информации о платах из каталога ESPHome.

    Параметр action поддерживает следующие действия:
      - "list"        — поиск/список плат (с фильтрами query, platform, variant, mcu, tag, limit, offset)
      - "get"         — получить полную информацию о плате (требует board_id)
      - "compatible"  — список совместимых/взаимозаменяемых плат (требует board_id)

    Параметры:
      - board_id:  ID платы, например "esp32dev", "nodemcuv2", "lolin32"
      - platform:  фильтр по платформе: "esp32", "esp8266", "rp2040", "nrf52840", "host", "bk72xx", "rtl87xx"
      - variant:   фильтр по варианту MCU/чипа (например "esp32c3", "esp32c6", "esp32s2", "esp32s3", "rp2040", "rp2350")
      - mcu:       фильтр по микроконтроллеру (например "esp32", "esp32c3", "rp2040")
      - tag:       фильтр по тегу плат (например "featured", "eth", "poe", "display", "battery")
      - query:     строка поиска (название платы, MCU, производитель)
      - limit:     максимальное количество результатов (по умолчанию 20)
      - offset:    смещение для пагинации (по умолчанию 0)
      - host:      хост сервера ESPHome
      - port:      сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "board_info_1"
    action = action.strip().lower()

    if action not in ("list", "get", "compatible"):
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: list, get, compatible."

    if action in ("get", "compatible") and not board_id:
        return f"Ошибка: для действия '{action}' необходимо указать параметр board_id."

    if action == "list":
        command = "boards/get_boards"
        args: dict = {"limit": limit}
        if offset > 0:
            args["offset"] = offset
        if query:
            args["query"] = query
        if platform:
            args["platform"] = platform
        if variant:
            args["variant"] = variant
        if mcu:
            args["mcu"] = mcu
        if tag:
            args["tag"] = tag
    elif action == "get":
        command = "boards/get_board"
        args = {"board_id": board_id}
    elif action == "compatible":
        command = "boards/get_compatible_boards"
        args = {"board_id": board_id}

    logger.info(f"get_board_info: action={action!r}, board_id={board_id!r}, query={query!r}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            await ws.send(json.dumps({
                "command": command,
                "message_id": msg_id,
                "args": args
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка команды boards ({action!r}): {data.get('details', data)}"

                result = data.get("result", {})

                if action == "list":
                    boards_list = result.get("boards", result) if isinstance(result, dict) else result
                    if not boards_list:
                        return "Платы по заданным критериям не найдены."
                    output = [f"### Каталог плат ESPHome (найдено: {len(boards_list)}):\n"]
                    for b in boards_list:
                        bid = b.get("id", "N/A")
                        bname = b.get("name", "N/A")
                        esp = b.get("esphome") or {}
                        bplatform = esp.get("platform") or b.get("platform", "?")
                        variant = esp.get("variant") or esp.get("board") or ""
                        variant_str = f", вариант: {variant}" if variant else ""
                        output.append(f"- `{bid}` — **{bname}** (платформа: {bplatform}{variant_str})")
                    return "\n".join(output)

                elif action == "get":
                    if not result:
                        return f"Плата `{board_id}` не найдена в каталоге ESPHome."
                    bname = result.get("name", "N/A")
                    hw = result.get("hardware", {}) or {}
                    esphome_cfg = result.get("esphome", {}) or {}
                    pins = result.get("pins", []) or []
                    featured = result.get("featured_components", []) or []

                    # MCU: берём из variant (esp32, esp32s3 и т.д.) или board
                    variant = esphome_cfg.get('variant', '')
                    board_val = esphome_cfg.get('board', board_id)

                    output = [f"### Плата: **{bname}** (`{board_id}`)\n"]
                    output.append(f"- **Платформа:** {esphome_cfg.get('platform', '?')}")
                    output.append(f"- **Вариант/MCU:** {variant or '?'} (board: `{board_val}`)")
                    output.append(f"- **Требует Wi-Fi:** {'Да' if result.get('requires_wifi') else 'Нет'}")
                    if result.get('description'):
                        output.append(f"- **Описание:** {result['description']}")
                    if result.get('docs_url'):
                        output.append(f"- **Документация:** {result['docs_url']}")

                    if hw:
                        flash = hw.get("flash", "?")
                        ram = hw.get("ram", "?")
                        output.append(f"- **Flash:** {flash}, **RAM:** {ram}")

                    if pins:
                        avail_pins = [p for p in pins if p.get("available", True) is not False]
                        unavail_pins = [p for p in pins if p.get("available", True) is False]
                        output.append(f"\n**Доступные контакты ({len(avail_pins)} из {len(pins)}):**")
                        for p in avail_pins[:12]:
                            label = p.get("label", f"GPIO{p.get('gpio', '?')}")
                            features = ", ".join(p.get("features", []))
                            notes = p.get("notes", "")
                            feat_str = f" [{features}]" if features else ""
                            note_str = f" — {notes}" if notes else ""
                            output.append(f"  - `{label}`{feat_str}{note_str}")
                        if unavail_pins:
                            unavail_labels = ", ".join(p.get('label', '') for p in unavail_pins[:6])
                            output.append(f"\n**⚠️ Занятые/недоступные пины ({len(unavail_pins)}):** {unavail_labels}")

                    if featured:
                        output.append(f"\n**Поддерживаемые компоненты ({len(featured)}):**")
                        for fc in featured[:8]:
                            fname = fc.get("name") or fc.get("component_id", "?")
                            output.append(f"  - {fname}")

                    return "\n".join(output)

                elif action == "compatible":
                    boards_list = result.get("boards", result) if isinstance(result, dict) else result
                    if not boards_list:
                        return f"Совместимые платы для `{board_id}` не найдены."
                    output = [f"### Совместимые платы для `{board_id}` ({len(boards_list)} шт.):\n"]
                    for b in boards_list:
                        bid = b.get("id", "N/A")
                        bname = b.get("name", "N/A")
                        output.append(f"- `{bid}` — **{bname}**")
                    return "\n".join(output)

    except Exception as e:
        error_msg = f"Критическая ошибка get_board_info ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def manage_build_jobs(
    action: str,
    configuration: str = "",
    job_id: str = "",
    status_filter: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 11 (P1): Управление очередью компиляции и сборки устройств ESPHome.

    Параметр action поддерживает следующие действия:
      - "list"         — список задач (с фильтрацией по status_filter и/или configuration)
      - "get"          — получить полную информацию о задаче (требует job_id)
      - "cancel"       — отменить задачу (требует job_id)
      - "clean"        — очистить каталог сборки конкретного устройства (требует configuration)
      - "reset_env"    — глобальный сброс .esphome/ (PlatformIO, external_components, build)
      - "clear"        — очистить историю завершенных задач из памяти (опционально status_filter)
      - "clear_queued" — отменить отложенное обновление устройства (deferred install, требует configuration)

    Параметры:
      - configuration:  имя YAML-файла устройства (например "test.yaml")
      - job_id:         идентификатор задачи (для get, cancel)
      - status_filter:  фильтр по статусу: "queued", "running", "completed", "failed", "cancelled"
      - host:           хост сервера ESPHome
      - port:           сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "build_jobs_1"
    action = action.strip().lower()

    valid_actions = ("list", "get", "cancel", "clean", "reset_env", "clear", "clear_queued")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: {', '.join(valid_actions)}."

    if action == "get" and not job_id:
        return "Ошибка: для действия 'get' необходимо указать параметр job_id."
    if action == "cancel" and not job_id:
        return "Ошибка: для действия 'cancel' необходимо указать параметр job_id."
    if action == "clean" and not configuration:
        return "Ошибка: для действия 'clean' необходимо указать параметр configuration."
    if action == "clear_queued" and not configuration:
        return "Ошибка: для действия 'clear_queued' необходимо указать параметр configuration."

    config_clean = resolve_configuration(configuration) if configuration else ""

    if action == "list":
        command = "firmware/get_jobs"
        args: dict = {}
        if status_filter:
            args["status"] = status_filter
        if config_clean:
            args["configuration"] = config_clean
    elif action == "get":
        command = "firmware/get_job"
        args = {"job_id": job_id}
    elif action == "cancel":
        command = "firmware/cancel"
        args = {"job_id": job_id}
    elif action == "clean":
        command = "firmware/clean"
        args = {"configuration": config_clean}
    elif action == "reset_env":
        command = "firmware/reset_build_env"
        args = {}
    elif action == "clear":
        command = "firmware/clear"
        args = {}
        if status_filter:
            args["status"] = status_filter
    elif action == "clear_queued":
        command = "firmware/clear_queued_update"
        args = {"configuration": config_clean}

    logger.info(f"manage_build_jobs: action={action!r}, configuration={config_clean!r}, job_id={job_id!r}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            await ws.send(json.dumps({
                "command": command,
                "message_id": msg_id,
                "args": args
            }))

            # clean и reset_env запускают Job — нужно ждать Job-result
            if action in ("clean", "reset_env"):
                job_id_from_resp = None
                while True:
                    res_raw = await ws.recv()
                    res_data = json.loads(res_raw)
                    if res_data.get("error_code"):
                        return f"Ошибка команды {action!r}: {res_data.get('details', res_data)}"
                    if res_data.get("message_id") == msg_id and "result" in res_data:
                        r = res_data["result"]
                        if r is None:
                            continue
                        job_id_from_resp = (r or {}).get("job_id")
                        break

                if not job_id_from_resp:
                    return f"Ошибка: не получен job_id для команды {action!r}."

                follow_id = "build_jobs_follow_1"
                await ws.send(json.dumps({
                    "command": "firmware/follow_job",
                    "message_id": follow_id,
                    "args": {"job_id": job_id_from_resp}
                }))

                follow_log = []
                async for message in ws:
                    fdata = json.loads(message)
                    if fdata.get("message_id") != follow_id:
                        continue
                    event = fdata.get("event")
                    if event == "output":
                        line_data = fdata.get("data", "")
                        line = (line_data.get("line", "") if isinstance(line_data, dict) else str(line_data)).strip()
                        if line:
                            follow_log.append(clean_ansi(line))
                    elif event == "result":
                        status_data = fdata.get("data", {})
                        success = (status_data.get("status") == "completed" or status_data.get("success") is True)
                        status_str = "УСПЕШНО ✅" if success else f"ОШИБКА ❌ ({status_data.get('status', '?')})"
                        tail = "\n".join(follow_log[-20:])
                        label = "очистку сборки" if action == "clean" else "сброс build-окружения"
                        return f"Статус ({label}): {status_str}\n\nЛог:\n{tail}"

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка команды {action!r}: {data.get('details', data)}"

                result = data.get("result")

                if action == "list":
                    jobs = result if isinstance(result, list) else []
                    if not jobs:
                        filter_str = f" (фильтр: {status_filter})" if status_filter else ""
                        return f"Задачи в очереди не найдены{filter_str}."
                    output = [f"### Задачи сборки ESPHome ({len(jobs)}):\n"]
                    for j in jobs:
                        jid = j.get("job_id", "?")
                        jtype = j.get("job_type", "?")
                        jstatus = j.get("status", "?")
                        jconfig = j.get("configuration", "")
                        progress = j.get("progress")
                        prog_str = f" [{progress}%]" if progress is not None else ""
                        output.append(f"- `{jid}` | {jtype} | **{jstatus}**{prog_str} — `{jconfig}`")
                    return "\n".join(output)

                elif action == "get":
                    if not result:
                        return f"Задача `{job_id}` не найдена."
                    jtype = result.get("job_type", "?")
                    jstatus = result.get("status", "?")
                    jconfig = result.get("configuration", "?")
                    progress = result.get("progress")
                    output_lines = result.get("output", []) or []
                    tail = "\n".join(output_lines[-20:]) if output_lines else "(лог пустой)"
                    prog_str = f"\n- **Прогресс:** {progress}%" if progress is not None else ""
                    return (
                        f"### Задача `{job_id}`:\n"
                        f"- **Тип:** {jtype}\n"
                        f"- **Статус:** **{jstatus}**\n"
                        f"- **Конфигурация:** `{jconfig}`"
                        f"{prog_str}\n\n"
                        f"**Последние строки лога:**\n{tail}"
                    )

                elif action == "cancel":
                    return f"✅ Задача `{job_id}` успешно отменена."

                elif action == "clear":
                    cleared_count = result.get("cleared") if isinstance(result, dict) else (len(result) if isinstance(result, list) else None)
                    count_str = f" ({cleared_count} шт.)" if cleared_count is not None else ""
                    return f"✅ История завершенных задач сборки успешно очищена{count_str}."

                elif action == "clear_queued":
                    return f"✅ Отложенное обновление для `{config_clean}` успешно сброшено."

    except Exception as e:
        error_msg = f"Критическая ошибка manage_build_jobs ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg

# ==========================================
# ФАЗА P2: ПАКЕТНЫЕ ОПЕРАЦИИ, АРХИВАЦИЯ, МЕТКИ И АУТЕНТИФИКАЦИЯ
# ==========================================

@mcp.tool()
async def batch_compile_and_flash(
    configurations: list[str],
    action: str = "install",
    port: str = "OTA",
    force_local: bool = False,
    bootloader: bool = False,
    host: str = DEFAULT_HOST,
    api_port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 12 (P2): Пакетная компиляция и/или прошивка нескольких устройств ESPHome.

    Параметр action поддерживает:
      - "compile" — пакетная компиляция без прошивки (firmware/compile_bulk)
      - "install" — пакетная компиляция + OTA/Serial-прошивка (firmware/install_bulk).
                    Для оффлайн-устройств обновление откладывается и применяется при следующем включении.

    Параметры:
      - configurations: список YAML-файлов устройств (например ["test.yaml", "ina226.yaml"])
      - port: "OTA" (по умолчанию для сетевой прошивки), IP-адрес или serial-порт
      - force_local: если True, принудительная локальная сборка без кэша/offloading
      - bootloader: если True, выполняет также прошивку загрузчика
      - host: хост сервера ESPHome (по умолчанию из .env или localhost)
      - api_port: сетевой порт WebSocket API ESPHome (по умолчанию из .env или 6052)
    """
    if not configurations:
        return "Ошибка: список configurations пустой."
    action = action.strip().lower()
    if action not in ("compile", "install"):
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: compile, install."

    command = "firmware/compile_bulk" if action == "compile" else "firmware/install_bulk"
    resolved_configs = [resolve_configuration(c) for c in configurations]
    args: dict = {"configurations": resolved_configs}
    if action == "install":
        args["port"] = port if port else "OTA"
    if force_local:
        args["force_local"] = True
    if bootloader:
        args["bootloader"] = True

    url = get_ws_url(host, api_port)
    msg_id = "batch_flash_1"

    logger.info(f"batch_compile_and_flash: action={action!r}, {len(configurations)} устройств...")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            await ws.send(json.dumps({
                "command": command,
                "message_id": msg_id,
                "args": args
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка пакетной операции {action!r}: {data.get('details', data)}"

                result = data.get("result", [])
                jobs = result if isinstance(result, list) else []

                if not jobs:
                    return f"Пакетная операция {action!r} не вернула задач."

                action_label = "компиляции" if action == "compile" else "прошивки"
                output = [f"### Пакетная {action_label} запущена для {len(jobs)} устройств:\n"]
                for j in jobs:
                    jid = j.get("job_id", "?")
                    jstatus = j.get("status", "?")
                    jconfig = j.get("configuration", "?")
                    jtype = j.get("job_type", "?")
                    deferred = j.get("is_deferred_install", False)
                    defer_str = " ⏸ (отложено до включения)" if deferred else ""
                    output.append(f"- `{jconfig}` → job `{jid}` | тип: {jtype} | статус: **{jstatus}**{defer_str}")

                output.append(f"\n💡 Для отслеживания используйте `manage_build_jobs action=get job_id=<id>`")
                return "\n".join(output)

    except Exception as e:
        error_msg = f"Критическая ошибка batch_compile_and_flash ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def archive_devices(
    action: str,
    configuration: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 13 (P2): Архивация и восстановление устройств ESPHome.

    Параметр action поддерживает следующие действия:
      - "archive"   — мягкое удаление: перемещение YAML в archive/, очистка сборки (обратимо)
      - "unarchive" — восстановление устройства из архива обратно в активные конфиги
      - "list"      — получить список всех архивных устройств
      - "purge"     — окончательно удалить архивный конфиг без возможности восстановления

    Параметры:
      - configuration: имя YAML-файла устройства (для archive, unarchive, purge)
    """
    action = action.strip().lower()
    valid = ("archive", "unarchive", "list", "purge")
    if action not in valid:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: {', '.join(valid)}."

    if action in ("archive", "unarchive", "purge") and not configuration:
        return f"Ошибка: для действия '{action}' необходимо указать параметр configuration."

    command_map = {
        "archive":   "devices/archive",
        "unarchive": "devices/unarchive",
        "list":      "devices/list_archived",
        "purge":     "devices/delete_archived",
    }
    command = command_map[action]
    args = {"configuration": configuration} if action != "list" else {}

    url = get_ws_url(host, port)
    msg_id = "archive_dev_1"

    logger.info(f"archive_devices: action={action!r}, configuration={configuration!r}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            await ws.send(json.dumps({
                "command": command,
                "message_id": msg_id,
                "args": args
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка команды {action!r}: {data.get('details', data)}"

                result = data.get("result")

                if action == "archive":
                    return f"✅ Устройство `{configuration}` перемещено в архив (мягкое удаление)."
                elif action == "unarchive":
                    return f"✅ Устройство `{configuration}` восстановлено из архива."
                elif action == "purge":
                    return f"✅ Архивная конфигурация `{configuration}` окончательно удалена."
                elif action == "list":
                    devices = result if isinstance(result, list) else []
                    if not devices:
                        return "Архив устройств пуст."
                    output = [f"### Архивные устройства ESPHome ({len(devices)}):\n"]
                    for d in devices:
                        cfg = d.get("configuration", "?")
                        name = d.get("name", "?")
                        friendly = d.get("friendly_name") or name
                        comment = d.get("comment", "")
                        cmt_str = f" — *{comment}*" if comment else ""
                        output.append(f"- **{friendly}** (`{cfg}`){cmt_str}")
                    output.append("\n💡 Используйте `archive_devices action=unarchive` для восстановления.")
                    return "\n".join(output)

    except Exception as e:
        error_msg = f"Критическая ошибка archive_devices ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def manage_device_labels(
    configuration: str,
    label_ids: list[str],
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 14 (P2): Управление метками (тегами) устройств ESPHome.

    Заменяет текущий набор меток устройства на указанный.
    Передайте пустой список [] для удаления всех меток.

    Параметры:
      - configuration: имя YAML-файла устройства (например "test.yaml")
      - label_ids:     список идентификаторов меток для установки (например ["room:living_room"])
    """
    url = get_ws_url(host, port)
    msg_id = "set_labels_1"

    logger.info(f"manage_device_labels: configuration={configuration!r}, labels={label_ids}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            await ws.send(json.dumps({
                "command": "devices/set_labels",
                "message_id": msg_id,
                "args": {"configuration": configuration, "label_ids": label_ids}
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка установки меток: {data.get('details', data)}"

                result = data.get("result", {}) or {}
                applied = result.get("labels", label_ids)
                if label_ids:
                    labels_str = ", ".join(f"`{l}`" for l in (applied if applied else label_ids))
                    return f"✅ Метки устройства `{configuration}` обновлены: {labels_str}"
                else:
                    return f"✅ Все метки устройства `{configuration}` удалены."

    except Exception as e:
        error_msg = f"Критическая ошибка manage_device_labels ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def manage_labels(
    action: str = "list",
    label_id: str = "",
    name: str = "",
    color: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 21 (P1): Управление глобальным каталогом меток устройств ESPHome.

    Позволяет агенту создавать, редактировать, просматривать и удалять метки
    (теги) парка устройств с цветовой кодировкой (#RRGGBB).

    Параметр action поддерживает:
      - "list"   — просмотр всех меток в каталоге (id, name, color)
      - "create" — создание новой метки (требует name, опционально color)
      - "update" — изменение названия или цвета метки (требует label_id, name/color)
      - "delete" — удаление метки из каталога и автоматическое снятие с устройств (требует label_id)

    Параметры:
      - label_id: уникальный ID метки (например "377c4f61e3f74fd8bf3ed7f0c44f71b9")
      - name:     название метки (например "Living Room", "Battery Powered")
      - color:    цвет метки в формате HEX (#rrggbb, например "#ff5733")
      - host:     хост сервера ESPHome
      - port:     сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "labels_tool_1"
    action = action.strip().lower()

    valid_actions = ("list", "create", "update", "delete")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: {', '.join(valid_actions)}."

    if action == "list":
        command = "labels/list"
        args = {}
    elif action == "create":
        if not name:
            return "Ошибка: для действия 'create' необходимо указать параметр 'name' (название метки)."
        command = "labels/create"
        args = {"name": name}
        if color:
            args["color"] = color if color.startswith("#") else f"#{color}"
    elif action == "update":
        if not label_id:
            return "Ошибка: для действия 'update' необходимо указать параметр 'label_id' (ID метки)."
        if not name and not color:
            return "Ошибка: для действия 'update' необходимо указать хотя бы один изменяемый параметр ('name' или 'color')."
        command = "labels/update"
        args = {"label_id": label_id}
        if name:
            args["name"] = name
        if color:
            args["color"] = color if color.startswith("#") else f"#{color}"
    elif action == "delete":
        if not label_id:
            return "Ошибка: для действия 'delete' необходимо указать параметр 'label_id' (ID метки)."
        command = "labels/delete"
        args = {"label_id": label_id}

    logger.info(f"manage_labels: action={action!r}, label_id={label_id!r}, name={name!r}, color={color!r}")

    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            await ws.send(json.dumps({
                "command": command,
                "message_id": msg_id,
                "args": args
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка команды labels ({action!r}): {data.get('details', data)}"

                result = data.get("result", {})

                if action == "list":
                    labels = result if isinstance(result, list) else []
                    if not labels:
                        return "Каталог меток пуст (метки пока не созданы)."
                    output = [f"### Каталог меток ESPHome ({len(labels)} шт.):\n"]
                    for lbl in labels:
                        lid = lbl.get("id", "N/A")
                        lname = lbl.get("name", lid)
                        lcolor = lbl.get("color", "")
                        color_str = f" `{lcolor}`" if lcolor else ""
                        output.append(f"- **{lname}**{color_str} — ID: `{lid}`")
                    return "\n".join(output)

                elif action == "create":
                    lid = result.get("id", "N/A") if isinstance(result, dict) else "N/A"
                    lname = result.get("name", name) if isinstance(result, dict) else name
                    lcolor = result.get("color", color) if isinstance(result, dict) else color
                    return f"✅ Метка `{lname}` ({lcolor}) успешно создана с ID: `{lid}`."

                elif action == "update":
                    lid = result.get("id", label_id) if isinstance(result, dict) else label_id
                    lname = result.get("name", name) if isinstance(result, dict) else name
                    lcolor = result.get("color", color) if isinstance(result, dict) else color
                    return f"✅ Метка `{lid}` успешно обновлена: имя='{lname}', цвет='{lcolor}'."

                elif action == "delete":
                    return f"✅ Метка `{label_id}` успешно удалена из каталога и снята со всех устройств."

    except Exception as e:
        error_msg = f"Критическая ошибка manage_labels ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def batch_manage_devices(
    action: str = "archive",
    configurations: list[str] | None = None,
    label_ids: list[str] | None = None,
    updates: list[dict] | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 22 (P1): Пакетные операции над группой устройств (Bulk Operations).

    Позволяет агенту массово удалять, архивировать или назначать метки сразу
    на множество устройств за один атомарный запрос.

    Параметр action поддерживает:
      - "archive"    — массовая архивация списка устройств (devices/archive_bulk)
      - "delete"     — массовое безвозвратное удаление списка устройств (devices/delete_bulk)
      - "set_labels" — массовое назначение меток:
                       либо единый список label_ids на все configurations,
                       либо детальный список updates=[{"configuration": "...", "label_ids": [...]}, ...]

    Параметры:
      - configurations: список имен конфигураций (например ["dev1.yaml", "dev2.yaml"])
      - label_ids:      список ID меток для применения ко всем указанным configurations
      - updates:        детальная структура для индивидуального назначения меток
      - host:           хост сервера ESPHome
      - port:           сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "bulk_tool_1"
    action = action.strip().lower()

    valid_actions = ("archive", "delete", "set_labels")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: archive, delete, set_labels."

    if action in ("archive", "delete"):
        if not configurations:
            return f"Ошибка: для действия '{action}' необходимо передать список 'configurations'."
        clean_configs = [resolve_configuration(c) for c in configurations]
        command = f"devices/{action}_bulk"
        args = {"configurations": clean_configs}

    elif action == "set_labels":
        if updates is not None:
            clean_updates = []
            for u in updates:
                if isinstance(u, dict) and "configuration" in u:
                    clean_updates.append({
                        "configuration": resolve_configuration(u["configuration"]),
                        "label_ids": u.get("label_ids", [])
                    })
            args = {"updates": clean_updates}
        elif configurations:
            lbls = label_ids or []
            args = {
                "updates": [
                    {"configuration": resolve_configuration(c), "label_ids": lbls}
                    for c in configurations
                ]
            }
        else:
            return "Ошибка: для действия 'set_labels' необходимо передать либо 'configurations' (и опционально 'label_ids'), либо детальный список 'updates'."
        command = "devices/set_labels_bulk"

    logger.info(f"batch_manage_devices: action={action!r}")

    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            await ws.send(json.dumps({
                "command": command,
                "message_id": msg_id,
                "args": args
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"Ошибка команды batch_manage_devices ({action!r}): {data.get('details', data)}"

                result = data.get("result", [])
                items = result if isinstance(result, list) else []

                success_count = sum(1 for item in items if isinstance(item, dict) and item.get("success", False))
                fail_count = len(items) - success_count

                output = [f"### Результат пакетной операции `{action}` (всего: {len(items)}, успешно: {success_count}, ошибок: {fail_count}):\n"]
                for item in items:
                    if isinstance(item, dict):
                        cfg = item.get("configuration", "N/A")
                        ok = item.get("success", False)
                        err = item.get("error", "")
                        status_str = "✅ Успешно" if ok else f"❌ Ошибка ({err})"
                        output.append(f"- `{cfg}`: {status_str}")
                    else:
                        output.append(f"- {item}")

                return "\n".join(output)

    except Exception as e:
        error_msg = f"Критическая ошибка batch_manage_devices ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def troubleshoot_device(
    configuration: str = "",
    action: str = "probe",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 23 (P2): Глубокая диагностика сети и доступности устройств ESPHome.

    Позволяет агенту выполнять детальную диагностику сетевой доступности устройств:
    одновременную проверку ICMP-пинга, DNS-резолвинга, mDNS/Zeroconf кэша и поиск сетевых аномалий,
    а также быстрый опрос статусов доступности всех устройств парка.

    Параметр action поддерживает:
      - "probe"  — глубокая диагностика конкретного устройства (devices/troubleshoot).
                   Требует параметр configuration.
      - "states" — получение таблицы онлайн/офлайн статусов всех устройств (devices/get_states).

    Параметры:
      - configuration: имя YAML-файла устройства (например "test.yaml", "ina226.yaml")
      - action:        режим работы ("probe" по умолчанию или "states")
      - host:          хост сервера ESPHome
      - port:          сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "troubleshoot_tool_1"
    action = action.strip().lower()

    valid_actions = ("probe", "states")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: probe, states."

    if action == "probe":
        if not configuration:
            return "Ошибка: для действия 'probe' необходимо указать параметр 'configuration' (YAML-файл устройства)."
        cfg_clean = resolve_configuration(configuration)
        command = "devices/troubleshoot"
        args = {"configuration": cfg_clean}
    elif action == "states":
        cfg_clean = ""
        command = "devices/get_states"
        args = {}

    logger.info(f"troubleshoot_device: action={action!r}, configuration={cfg_clean!r}")

    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            await ws.send(json.dumps({
                "command": command,
                "message_id": msg_id,
                "args": args
            }))

            # Для troubleshoot даем до 35 секунд таймаута на сетевые проверки (DNS + mDNS + Ping)
            timeout = 35.0 if action == "probe" else 10.0
            raw_msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(raw_msg)

            if data.get("error_code"):
                return f"Ошибка команды troubleshoot ({action!r}): {data.get('details', data)}"

            result = data.get("result", {})

            if action == "states":
                states_dict = result if isinstance(result, dict) else {}
                online_devs = [k for k, v in states_dict.items() if v == "online"]
                offline_devs = [k for k, v in states_dict.items() if v == "offline"]
                unknown_devs = [k for k, v in states_dict.items() if v not in ("online", "offline")]

                output = [f"### 📊 Статусы сетевой доступности устройств ESPHome ({len(states_dict)} устройств):\n"]
                output.append(f"🟢 **Онлайн ({len(online_devs)}):**")
                if online_devs:
                    for d in sorted(online_devs):
                        output.append(f"- `{d}`")
                else:
                    output.append("- *Нет устройств онлайн*")

                output.append(f"\n🔴 **Офлайн ({len(offline_devs)}):**")
                if offline_devs:
                    for d in sorted(offline_devs):
                        output.append(f"- `{d}`")
                else:
                    output.append("- *Нет устройств офлайн*")

                if unknown_devs:
                    output.append(f"\n⚪ **Неизвестно ({len(unknown_devs)}):**")
                    for d in sorted(unknown_devs):
                        output.append(f"- `{d}`: {states_dict[d]}")

                output.append("\n💡 Для детальной диагностики устройства используйте `troubleshoot_device configuration=<file.yaml>`")
                return "\n".join(output)

            elif action == "probe":
                tb = result if isinstance(result, dict) else {}
                cfg = tb.get("configuration", cfg_clean)
                address = tb.get("address", "N/A")
                dns_res = tb.get("dns_resolved", False)
                dns_ips = tb.get("dns_addresses") or []
                mdns_ips = tb.get("mdns_addresses") or []
                mdns_ptr = tb.get("mdns_has_live_anchor_ptr", False)
                mdns_trace = tb.get("mdns_has_cached_trace", False)
                ping_att = tb.get("ping_attempted", False)
                ping_target = tb.get("ping_target", "N/A")
                ping_source = tb.get("ping_target_source", "N/A")
                ping_rtt = tb.get("ping_rtt_ms")
                icmp_avail = tb.get("icmp_available", True)
                zc_running = tb.get("zeroconf_running", True)

                # Анализ статуса
                is_online = ping_rtt is not None and ping_rtt > 0

                output = [f"### 🔍 Сетевая диагностика устройства `{cfg}`:\n"]
                output.append(f"- **Целевой адрес (Address):** `{address}`")
                status_icon = "🟢 **ДОСТУПНО (Online)**" if is_online else "🔴 **НЕДОСТУПНО (Offline)**"
                output.append(f"- **Итоговый статус:** {status_icon}\n")

                output.append("#### 📡 Результаты проверок:")

                # DNS
                if dns_res:
                    ips_str = ", ".join(f"`{ip}`" for ip in dns_ips) if dns_ips else "IP не вернулся"
                    output.append(f"- **DNS разрешение:** ✅ Успешно ({ips_str})")
                else:
                    output.append(f"- **DNS разрешение:** ❌ Не удалось разрешить имя")

                # mDNS
                if mdns_ips:
                    mips_str = ", ".join(f"`{ip}`" for ip in mdns_ips)
                    ptr_str = " (live PTR: ✅)" if mdns_ptr else ""
                    output.append(f"- **mDNS / Zeroconf:** ✅ Обнаружен ({mips_str}){ptr_str}")
                else:
                    trace_str = " (есть след в кэше)" if mdns_trace else " (записи не найдены)"
                    output.append(f"- **mDNS / Zeroconf:** ❌ Локальные анонсы не получены{trace_str}")

                # Ping
                if ping_att:
                    if is_online:
                        output.append(f"- **ICMP Ping:** ✅ Доступен (`{ping_target}`, источник: {ping_source}) — RTT: **{ping_rtt:.2f} ms**")
                    else:
                        output.append(f"- **ICMP Ping:** ❌ Нет ответа (`{ping_target}`, источник: {ping_source})")
                else:
                    output.append("- **ICMP Ping:** ⚪ Не выполнялся")

                # Сервисы
                srv_info = []
                if not icmp_avail:
                    srv_info.append("ICMP-сокеты недоступны на сервере")
                if not zc_running:
                    srv_info.append("Zeroconf демон не запущен")
                if srv_info:
                    output.append(f"- **Внимание к службам хоста:** {', '.join(srv_info)}")

                # Экспертное заключение
                output.append("\n#### 💡 Заключение диагностики:")
                if is_online:
                    output.append(f"Устройство активно в сети, откликается на сетевые запросы с задержкой {ping_rtt:.2f} ms.")
                elif dns_res and not is_online:
                    output.append(f"Устройство разрешается через DNS ({', '.join(dns_ips)}), но не отвечает на пинг и mDNS. Возможные причины: устройство обесточено, зависло или трафик блокируется файрволом/изоляцией клиентов в Wi-Fi сети.")
                elif not dns_res and not mdns_ips:
                    output.append("Устройство полностью отсутствует в локальной сети (не разрешается по DNS и не анонсирует себя по mDNS).")
                else:
                    output.append("Обнаружены частичные сетевые аномалии при опросе устройства.")

                return "\n".join(output)

    except Exception as e:
        error_msg = f"Критическая ошибка troubleshoot_device ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


def find_git_repo(config_dir: str = "") -> tuple[str, str] | None:
    """
    Определяет корневую директорию Git-репозитория и относительный префикс каталога конфигураций.
    Возвращает кортеж (repo_root, prefix_inside_repo) или None, если репозиторий не найден.
    """
    candidates = []
    if config_dir:
        candidates.append(os.path.expanduser(config_dir))

    # Стандартные пути поиска каталога конфигураций ESPHome
    candidates.extend([
        "/Users/andreyzolotnitskiy/Documents/github/esphome/config",
        os.path.expanduser("~/Documents/github/esphome/config"),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "esphome", "config")),
        "/Users/andreyzolotnitskiy/Documents/github/esphome",
        os.getcwd()
    ])

    for cand in candidates:
        if not os.path.exists(cand):
            continue
        try:
            res = subprocess.run(
                ["git", "-C", cand, "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True
            )
            repo_root = res.stdout.strip()
            if repo_root and os.path.isdir(repo_root):
                cand_abs = os.path.abspath(cand)
                rel_prefix = os.path.relpath(cand_abs, repo_root)
                prefix = "" if rel_prefix == "." else rel_prefix.strip("/")
                return (repo_root, prefix)
        except Exception:
            continue
    return None


@mcp.tool()
async def manage_version_history(
    action: str = "log",
    configuration: str = "",
    sha: str = "",
    sha_compare: str = "",
    max_count: int = 10,
    config_dir: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 24 (P2): История версий конфигураций и откат изменений (Git Version History).

    Позволяет агенту просматривать историю изменений YAML-конфигураций ESPHome,
    получать diff между ревизиями, просматривать содержимое старых версий,
    находить удаленные конфигурации и восстанавливать файлы до выбранного коммита.

    Параметр action поддерживает:
      - "log"      — история коммитов по файлу или всему каталогу (sha, дата, автор, сообщение)
      - "diff"     — просмотр изменений (рабочая копия vs HEAD, либо между sha и sha_compare)
      - "show"     — чтение содержимого файла конфигурации на момент коммита sha
      - "deleted"  — поиск удаленных ранее файлов конфигураций в истории Git
      - "restore"  — откат/восстановление файла до состояния коммита sha (через API устройств)

    Параметры:
      - action:        режим работы ("log", "diff", "show", "deleted", "restore")
      - configuration: имя YAML-файла устройства (например "test.yaml", "liligo-t-internet.yaml")
      - sha:           хэш Git-коммита (например "a11f8af", "HEAD~1")
      - sha_compare:   второй хэш коммита для сравнения в diff (опционально)
      - max_count:     максимальное количество записей в истории (по умолчанию 10)
      - config_dir:    путь к каталогу конфигураций (опционально, определяется автоматически)
      - host:          хост сервера ESPHome
      - port:          сетевой порт WebSocket API ESPHome
    """
    action = action.strip().lower()
    valid_actions = ("log", "list", "diff", "show", "get", "deleted", "list_deleted", "restore")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: log, diff, show, deleted, restore."

    cfg_clean = resolve_configuration(configuration) if configuration else ""

    if action in ("show", "get") and not cfg_clean:
        return "Ошибка: для действия 'show' необходимо указать параметр 'configuration' (YAML-файл устройства)."
    if action == "restore":
        if not cfg_clean:
            return "Ошибка: для действия 'restore' необходимо указать параметр 'configuration' (YAML-файл устройства)."
        if not sha.strip():
            return "Ошибка: для действия 'restore' необходимо указать хэш коммита в параметре 'sha'."

    repo_info = find_git_repo(config_dir)
    if not repo_info:
        return "Ошибка: не удалось обнаружить Git-репозиторий с конфигурациями ESPHome. Укажите путь через параметр 'config_dir'."

    repo_root, prefix = repo_info
    rel_file = f"{prefix}/{cfg_clean}".lstrip("/") if cfg_clean else ""

    logger.info(f"manage_version_history: action={action!r}, configuration={cfg_clean!r}, repo={repo_root!r}")

    try:
        if action in ("log", "list"):
            count = max(1, min(max_count, 100))
            cmd = ["git", "-C", repo_root, "log", f"-n{count}", "--pretty=format:%h|%an|%ad|%s", "--date=short"]
            if rel_file:
                cmd.extend(["--", rel_file])
            elif prefix:
                cmd.extend(["--", f"{prefix}/*.yaml"])

            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                return f"Ошибка выполнения git log: {res.stderr.strip()}"

            lines = [l.strip() for l in res.stdout.strip().split("\n") if l.strip()]
            if not lines:
                target_str = f"`{cfg_clean}`" if cfg_clean else "каталога конфигураций"
                return f"История изменений для {target_str} не найдена или файл еще не был закоммичен."

            target_header = f"конфигурации `{cfg_clean}`" if cfg_clean else "всех конфигураций ESPHome"
            output = [f"### 📜 История версий {target_header} ({len(lines)} коммитов):\n"]
            for line in lines:
                parts = line.split("|", 3)
                if len(parts) == 4:
                    c_sha, c_author, c_date, c_msg = parts
                    output.append(f"- `{c_sha}` | **{c_date}** | {c_author} — *{c_msg}*")
                else:
                    output.append(f"- {line}")

            output.append("\n💡 Для просмотра изменений используйте `manage_version_history action=diff sha=<хэш>`")
            return "\n".join(output)

        elif action == "diff":
            cmd = ["git", "-C", repo_root, "diff"]
            if sha and sha_compare:
                cmd.extend([sha_compare, sha])
            elif sha:
                cmd.append(sha)
            else:
                cmd.append("HEAD")

            if rel_file:
                cmd.extend(["--", rel_file])
            elif prefix:
                cmd.extend(["--", f"{prefix}/*.yaml"])

            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                return f"Ошибка выполнения git diff: {res.stderr.strip()}"

            diff_text = res.stdout.strip()
            if not diff_text:
                target_str = f"`{cfg_clean}`" if cfg_clean else "каталога конфигураций"
                ref_str = f"({sha_compare} ↔ {sha})" if (sha and sha_compare) else (f"(ревизия {sha})" if sha else "(HEAD vs Working Tree)")
                return f"Нет различий (diff) для {target_str} {ref_str} — изменения отсутствуют."

            target_header = f"`{cfg_clean}`" if cfg_clean else "конфигураций"
            return f"### 🔀 Различия (diff) для {target_header}:\n\n```diff\n{diff_text}\n```"

        elif action in ("show", "get"):
            if not cfg_clean:
                return "Ошибка: для действия 'show' необходимо указать параметр 'configuration' (YAML-файл устройства)."
            sha_target = sha.strip() or "HEAD"
            git_path = f"{sha_target}:{rel_file}"

            res = subprocess.run(["git", "-C", repo_root, "show", git_path], capture_output=True, text=True)
            if res.returncode != 0:
                return f"Ошибка чтения ревизии {git_path}: {res.stderr.strip()}"

            return f"### 📄 Содержимое `{cfg_clean}` на момент коммита `{sha_target}`:\n\n```yaml\n{res.stdout.strip()}\n```"

        elif action in ("deleted", "list_deleted"):
            count = max(1, min(max_count, 100))
            pattern = f"{prefix}/*.yaml" if prefix else "*.yaml"
            cmd = ["git", "-C", repo_root, "log", "--diff-filter=D", "--summary", f"-n{count}", "--", pattern]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                return f"Ошибка поиска удаленных файлов: {res.stderr.strip()}"

            raw_out = res.stdout.strip()
            if not raw_out:
                return "В истории Git не найдено записей об удалении YAML-конфигураций."

            return f"### 🗑 Удаленные конфигурации в истории Git:\n\n```text\n{raw_out}\n```\n\n💡 Для восстановления удаленного файла используйте:\n`manage_version_history action=\"restore\" configuration=\"<имя_файла>\" sha=\"<хэш_коммита~1>\"`"

        elif action == "restore":
            if not cfg_clean:
                return "Ошибка: для действия 'restore' необходимо указать параметр 'configuration' (YAML-файл устройства)."
            if not sha.strip():
                return "Ошибка: для действия 'restore' необходимо указать хэш коммита в параметре 'sha'."

            sha_target = sha.strip()
            git_path = f"{sha_target}:{rel_file}"

            res = subprocess.run(["git", "-C", repo_root, "show", git_path], capture_output=True, text=True)
            if res.returncode != 0:
                return f"Ошибка извлечения версии `{git_path}` для отката: {res.stderr.strip()}"

            restored_content = res.stdout
            # Применяем восстановленное содержимое через API устройства
            apply_res = await manage_device_config(
                action="update",
                configuration=cfg_clean,
                content=restored_content,
                host=host,
                port=port
            )

            # Если файл ранее был удален и не найден, создаем его заново
            if "not found" in apply_res.lower() or "не найден" in apply_res.lower():
                apply_res = await manage_device_config(
                    action="create",
                    configuration=cfg_clean,
                    content=restored_content,
                    host=host,
                    port=port
                )

            if "ошибка" in apply_res.lower() and "успешно" not in apply_res.lower():
                return f"Ошибка применения восстановленной конфигурации `{cfg_clean}`: {apply_res}"

            return f"### 🔄 Восстановление конфигурации `{cfg_clean}`:\n\n✅ Файл `{cfg_clean}` успешно восстановлен до состояния коммита `{sha_target}` и сохранен через API ESPHome!"

    except Exception as e:
        error_msg = f"Критическая ошибка manage_version_history: {str(e)}"
        logger.error(error_msg)
        return error_msg


def apply_yaml_diff(content: str, diff: dict) -> str:
    """
    Применяет diff от сервера ESPHome (fromLine, toLine, replacement) к YAML-тексту.
    Строки в diff 1-индексированы.
    """
    from_line = max(1, diff.get("fromLine", 1)) - 1  # 1-based -> 0-based
    to_line = max(0, diff.get("toLine", 0)) - 1
    replacement = diff.get("replacement", "")

    lines = content.splitlines(keepends=True)
    if to_line < from_line:
        # Вставка перед from_line
        new_lines = lines[:from_line] + [replacement] + lines[from_line:]
    else:
        # Замена диапазона [from_line, to_line]
        new_lines = lines[:from_line] + [replacement] + lines[to_line + 1:]
    return "".join(new_lines)


@mcp.tool()
async def manage_automations(
    action: str = "parse",
    configuration: str = "",
    component_id: str = "",
    trigger: str = "",
    kind: str = "component_on",
    automation: dict | None = None,
    apply: bool = False,
    query: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 25 (P2): Управление AST автоматизаций ESPHome (визуальный редактор логики).

    Позволяет агенту инспектировать, парсить, валидировать, создавать, модифицировать
    и удалять блоки автоматизаций (on_press, on_turn_on, on_state, interval, script и др.)
    с точечной вставкой в YAML-файл без нарушения структуры и форматирования остального кода.

    Параметр action поддерживает:
      - "parse"       — разбор и вывод всех автоматизаций устройства в виде AST дерева (automations/parse).
                        Требует: configuration.
      - "available"   — список доступных сущностей, триггеров и действий для устройства (automations/get_available).
                        Требует: configuration.
      - "triggers"    — глобальный каталог доступных триггеров ESPHome (automations/get_triggers).
                        Опционально: query (фильтр по имени/домену).
      - "actions"     — глобальный каталог доступных действий ESPHome (automations/get_actions).
                        Опционально: query (фильтр по имени/домену).
      - "conditions"  — глобальный каталог доступных условий ESPHome (automations/get_conditions).
                        Опционально: query (фильтр по имени/домену).
      - "upsert"      — точечное добавление или обновление блока автоматизации в YAML-файле (automations/upsert).
                        Требует: configuration, component_id, trigger, automation (структура триггера и действий).
                        Опционально: kind ("component_on" по умолчанию), apply (True для автоматического сохранения).
      - "delete"      — точечное удаление блока автоматизации из YAML-файла (automations/delete).
                        Требует: configuration, component_id, trigger.
                        Опционально: kind ("component_on" по умолчанию), apply (True для автоматического сохранения).

    Параметры:
      - action:        режим работы ("parse", "available", "triggers", "actions", "conditions", "upsert", "delete")
      - configuration: имя YAML-файла устройства (например "test.yaml")
      - component_id:  ID сущности/компонента (например "relay_switch", "test_button")
      - trigger:       имя триггера (например "on_turn_on", "on_press", "on_value")
      - kind:          тип обработчика ("component_on" по умолчанию, "interval", "script" и др.)
      - automation:    словарь описания автоматизации (trigger_id, actions, trigger_params)
      - apply:         флаг автоматического применения diff в файл конфигурации (по умолчанию False)
      - query:         поисковый фильтр для каталогов triggers/actions/conditions
      - host:          хост сервера ESPHome
      - port:          сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "automations_tool_1"
    action = action.strip().lower()

    valid_actions = ("parse", "available", "triggers", "actions", "conditions", "upsert", "delete")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: parse, available, triggers, actions, conditions, upsert, delete."

    cfg_clean = resolve_configuration(configuration) if configuration else ""

    if action in ("parse", "available") and not cfg_clean:
        return f"Ошибка: для действия '{action}' необходимо указать параметр 'configuration' (YAML-файл устройства)."

    if action == "upsert":
        if not cfg_clean:
            return "Ошибка: для действия 'upsert' необходимо указать параметр 'configuration' (YAML-файл устройства)."
        if not component_id.strip():
            return "Ошибка: для действия 'upsert' необходимо указать параметр 'component_id' (ID компонента/сущности)."
        if not trigger.strip():
            return "Ошибка: для действия 'upsert' необходимо указать параметр 'trigger' (имя триггера, например 'on_turn_on')."
        if not automation or not isinstance(automation, dict):
            return "Ошибка: для действия 'upsert' необходимо указать словарь 'automation' с описанием триггера и действий."

    if action == "delete":
        if not cfg_clean:
            return "Ошибка: для действия 'delete' необходимо указать параметр 'configuration' (YAML-файл устройства)."
        if not component_id.strip():
            return "Ошибка: для действия 'delete' необходимо указать параметр 'component_id' (ID компонента/сущности)."
        if not trigger.strip():
            return "Ошибка: для действия 'delete' необходимо указать параметр 'trigger' (имя триггера, например 'on_turn_on')."

    logger.info(f"manage_automations: action={action!r}, configuration={cfg_clean!r}, component_id={component_id!r}")

    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            if action == "triggers":
                await ws.send(json.dumps({"command": "automations/get_triggers", "message_id": msg_id, "args": {}}))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка получения триггеров: {data.get('details', data)}"
                items = data.get("result", []) or []
                if query:
                    q = query.strip().lower()
                    items = [it for it in items if q in it.get("id", "").lower() or q in it.get("name", "").lower() or q in it.get("domain", "").lower()]

                output = [f"### ⚡ Каталог триггеров автоматизаций ESPHome ({len(items)} шт.):\n"]
                for it in items[:50]:
                    t_id = it.get("id", "")
                    name = it.get("name", "")
                    domain = it.get("domain", "")
                    desc = it.get("description", "")
                    output.append(f"- **`{t_id}`** ({name}, домен: `{domain}`): {desc}")
                return "\n".join(output)

            elif action == "actions":
                await ws.send(json.dumps({"command": "automations/get_actions", "message_id": msg_id, "args": {}}))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка получения действий: {data.get('details', data)}"
                items = data.get("result", []) or []
                if query:
                    q = query.strip().lower()
                    items = [it for it in items if q in it.get("id", "").lower() or q in it.get("name", "").lower() or q in it.get("domain", "").lower()]

                output = [f"### 🎬 Каталог действий (Actions) ESPHome ({len(items)} шт.):\n"]
                for it in items[:50]:
                    a_id = it.get("id", "")
                    name = it.get("name", "")
                    domain = it.get("domain", "")
                    desc = it.get("description", "")
                    output.append(f"- **`{a_id}`** ({name}, домен: `{domain}`): {desc}")
                return "\n".join(output)

            elif action == "conditions":
                await ws.send(json.dumps({"command": "automations/get_conditions", "message_id": msg_id, "args": {}}))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка получения условий: {data.get('details', data)}"
                items = data.get("result", []) or []
                if query:
                    q = query.strip().lower()
                    items = [it for it in items if q in it.get("id", "").lower() or q in it.get("name", "").lower() or q in it.get("domain", "").lower()]

                output = [f"### ❓ Каталог условий (Conditions) ESPHome ({len(items)} шт.):\n"]
                for it in items[:50]:
                    c_id = it.get("id", "")
                    name = it.get("name", "")
                    domain = it.get("domain", "")
                    desc = it.get("description", "")
                    output.append(f"- **`{c_id}`** ({name}, домен: `{domain}`): {desc}")
                return "\n".join(output)

            elif action == "available":
                await ws.send(json.dumps({"command": "automations/get_available", "message_id": msg_id, "args": {"configuration": cfg_clean}}))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка получения доступных компонентов ({cfg_clean}): {data.get('details', data)}"
                res = data.get("result", {}) or {}
                devs = res.get("devices", [])
                scripts = res.get("scripts", [])

                output = [f"### 📋 Доступные сущности для автоматизаций в `{cfg_clean}`:\n"]
                output.append(f"**Сущности / Компоненты ({len(devs)}):**")
                for d in devs:
                    d_id = d.get("id", "")
                    cid = d.get("component_id", "")
                    name = d.get("name") or d.get("title") or "N/A"
                    output.append(f"- ID: **`{d_id}`** | Тип: `{cid}` | Имя: *{name}*")

                if scripts:
                    output.append(f"\n**Скрипты ({len(scripts)}):**")
                    for s in scripts:
                        output.append(f"- `{s}`")

                return "\n".join(output)

            elif action == "parse":
                await ws.send(json.dumps({"command": "automations/parse", "message_id": msg_id, "args": {"configuration": cfg_clean}}))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка парсинга автоматизаций ({cfg_clean}): {data.get('details', data)}"
                items = data.get("result", []) or []
                if not items:
                    return f"В конфигурации `{cfg_clean}` не обнаружено объявленных блоков автоматизаций."

                output = [f"### ⚡ Автоматизации устройства `{cfg_clean}` (найдено: {len(items)}):\n"]
                for i, item in enumerate(items, 1):
                    lbl = item.get("label", "Автоматизация")
                    loc = item.get("location", {})
                    f_line = item.get("from_line", "?")
                    t_line = item.get("to_line", "?")
                    auto = item.get("automation", {})
                    trig_id = auto.get("trigger_id", "N/A")
                    actions_list = auto.get("actions", [])
                    raw_yaml = item.get("raw_yaml", "").strip()

                    output.append(f"#### {i}. {lbl} (строки {f_line}-{t_line}):")
                    output.append(f"- **Компонент:** `{loc.get('component_id', 'N/A')}` (триггер: `{loc.get('trigger', 'N/A')}`, kind: `{loc.get('kind', 'N/A')}`)")
                    output.append(f"- **Тип триггера:** `{trig_id}`")
                    output.append(f"- **Количество действий:** {len(actions_list)}")
                    if actions_list:
                        for a in actions_list:
                            output.append(f"  - `{a.get('action_id', 'N/A')}`: {a.get('params', {})}")
                    if raw_yaml:
                        output.append(f"\n```yaml\n{raw_yaml}\n```\n")

                return "\n".join(output)

            elif action == "upsert":
                location_dict = {
                    "component_id": component_id.strip(),
                    "trigger": trigger.strip(),
                    "kind": kind.strip() or "component_on"
                }
                await ws.send(json.dumps({
                    "command": "automations/upsert",
                    "message_id": msg_id,
                    "args": {
                        "configuration": cfg_clean,
                        "location": location_dict,
                        "automation": automation
                    }
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка добавления автоматизации upsert ({cfg_clean}): {data.get('details', data)}"

                res = data.get("result", {}) or {}
                diff = res.get("yaml_diff", {})
                if not diff:
                    return f"Сервер не сформировал изменений (diff) для автоматизации `{trigger}` компонента `{component_id}`."

                diff_rep = diff.get("replacement", "").strip()

                if not apply:
                    return (
                        f"### 🔍 Предпросмотр вставки автоматизации в `{cfg_clean}` (Dry-Run):\n\n"
                        f"- **Компонент:** `{component_id}` | **Триггер:** `{trigger}`\n"
                        f"- **Целевые строки:** {diff.get('fromLine')}-{diff.get('toLine')}\n\n"
                        f"```yaml\n{diff_rep}\n```\n\n"
                        f"💡 Для сохранения изменений передайте `apply=True`."
                    )

                # Получаем текущий конфиг
                await ws.send(json.dumps({"command": "devices/get_config", "message_id": "cfg_get", "args": {"configuration": cfg_clean}}))
                cfg_data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if cfg_data.get("error_code"):
                    return f"Ошибка чтения конфигурации перед применением: {cfg_data.get('details', cfg_data)}"
                raw_res = cfg_data.get("result", "")
                curr_content = raw_res if isinstance(raw_res, str) else (raw_res.get("content", "") if isinstance(raw_res, dict) else "")

                new_content = apply_yaml_diff(curr_content, diff)
                await ws.send(json.dumps({
                    "command": "devices/update_config",
                    "message_id": "cfg_upd",
                    "args": {"configuration": cfg_clean, "content": new_content}
                }))
                upd_data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if upd_data.get("error_code"):
                    return f"Ошибка сохранения конфигурации с новой автоматизацией: {upd_data.get('details', upd_data)}"

                return (
                    f"### ⚡ Автоматизация успешно добавлена в `{cfg_clean}`!\n\n"
                    f"- **Компонент:** `{component_id}` | **Триггер:** `{trigger}`\n"
                    f"- **Вставленный YAML блок:**\n```yaml\n{diff_rep}\n```"
                )

            elif action == "delete":
                location_dict = {
                    "component_id": component_id.strip(),
                    "trigger": trigger.strip(),
                    "kind": kind.strip() or "component_on"
                }
                await ws.send(json.dumps({
                    "command": "automations/delete",
                    "message_id": msg_id,
                    "args": {
                        "configuration": cfg_clean,
                        "location": location_dict
                    }
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка удаления автоматизации ({cfg_clean}): {data.get('details', data)}"

                res = data.get("result", {}) or {}
                diff = res.get("yaml_diff", {})
                if not diff:
                    return f"Сервер не сформировал изменений (diff) для удаления автоматизации `{trigger}` компонента `{component_id}`."

                if not apply:
                    return (
                        f"### 🔍 Предпросмотр удаления автоматизации из `{cfg_clean}` (Dry-Run):\n\n"
                        f"- **Компонент:** `{component_id}` | **Триггер:** `{trigger}`\n"
                        f"- **Удаляемые строки:** {diff.get('fromLine')}-{diff.get('toLine')}\n\n"
                        f"💡 Для сохранения изменений передайте `apply=True`."
                    )

                # Получаем текущий конфиг
                await ws.send(json.dumps({"command": "devices/get_config", "message_id": "cfg_get", "args": {"configuration": cfg_clean}}))
                cfg_data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if cfg_data.get("error_code"):
                    return f"Ошибка чтения конфигурации перед удалением: {cfg_data.get('details', cfg_data)}"
                raw_res = cfg_data.get("result", "")
                curr_content = raw_res if isinstance(raw_res, str) else (raw_res.get("content", "") if isinstance(raw_res, dict) else "")

                new_content = apply_yaml_diff(curr_content, diff)
                await ws.send(json.dumps({
                    "command": "devices/update_config",
                    "message_id": "cfg_upd",
                    "args": {"configuration": cfg_clean, "content": new_content}
                }))
                upd_data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if upd_data.get("error_code"):
                    return f"Ошибка сохранения конфигурации после удаления: {upd_data.get('details', upd_data)}"

                return f"### 🗑 Автоматизация `{trigger}` компонента `{component_id}` успешно удалена из `{cfg_clean}`!"

    except Exception as e:
        error_msg = f"Критическая ошибка manage_automations ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def authenticate_esphome(
    username: str = "",
    password: str = "",
    token: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 15 (P2): Аутентификация на защищённом ESPHome-сервере (requires_auth=true).

    Используйте, если ESPHome запущен с параметрами --username/--password или
    переменными окружения ESPHOME_USERNAME/ESPHOME_PASSWORD.

    Варианты аутентификации:
      1. По логину и паролю: укажите username и password
      2. По токену (повторный вход): укажите только token

    Возвращает токен сессии (expires_at), который можно использовать для повторного входа.
    """
    if not token and not (username and password):
        return "Ошибка: необходимо указать либо (username + password), либо token."

    url = get_ws_url(host, port)
    p = port if port is not None else DEFAULT_PORT
    msg_id = "auth_login_1"

    args: dict = {}
    if token:
        args = {"token": token}
    else:
        args = {"username": username, "password": password}

    logger.info(f"authenticate_esphome: host={host!r}, by_token={bool(token)}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)

            if not first_msg.get("requires_auth"):
                return f"ℹ️ ESPHome на {host}:{p} не требует аутентификации (requires_auth=false). Вход не нужен."

            await ws.send(json.dumps({
                "command": "auth/login",
                "message_id": msg_id,
                "args": args
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    code = data.get("error_code", "")
                    if code == "not_authenticated":
                        return "❌ Аутентификация не удалась: неверный логин или пароль."
                    if code == "rate_limited":
                        return "❌ Слишком много попыток входа. Повторите через 5 минут."
                    return f"❌ Ошибка аутентификации: {data.get('details', data)}"

                result = data.get("result", {}) or {}
                new_token = result.get("token", "")
                expires_at = result.get("expires_at", "")

                return (
                    f"✅ Аутентификация успешна на {host}:{p}\n"
                    f"- **Токен:** `{new_token}`\n"
                    f"- **Действителен до:** {expires_at}\n\n"
                    f"💡 Сохраните токен для повторного входа без пароля (параметр token)."
                )

    except Exception as e:
        error_msg = f"Критическая ошибка authenticate_esphome ({url}): {str(e)}"
        logger.error(error_msg)
@mcp.tool()
async def get_server_version() -> str:
    """
    Инструмент 16: Получение информации о версии MCP-сервера и протоколе.

    Возвращает текущую версию сервера (SemVer), статус компонентов и базовую конфигурацию.
    """
    return (
        f"### ESPHome Local MCP Server\n"
        f"- **Версия сервера:** `v{__version__}`\n"
        f"- **Стандарт версионирования:** Semantic Versioning 2.0.0 (SemVer)\n"
        f"- **Хост по умолчанию:** `{DEFAULT_HOST}:{DEFAULT_PORT}`\n"
        f"- **Транспорт:** stdio\n"
        f"- **Количество инструментов:** 16"
    )

if __name__ == "__main__":
    logger.info(f"Запуск ESPHome Local MCP Server v{__version__} (stdio)...")
    mcp.run(transport='stdio')

