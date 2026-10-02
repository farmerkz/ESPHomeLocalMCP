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
import hashlib
import urllib.request
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

def get_http_url(host: str | None = None, port: int | None = None, ssl: bool | None = None) -> str:
    """
    Формирует базовый HTTP/HTTPS URL подключения к ESPHome API.
    """
    h = host if host else DEFAULT_HOST
    p = port if port is not None else DEFAULT_PORT

    if ssl is not None:
        use_ssl = ssl
    elif DEFAULT_SSL is not None:
        use_ssl = DEFAULT_SSL
    else:
        use_ssl = p in (443, 8443)

    scheme = "https" if use_ssl else "http"
    return f"{scheme}://{h}:{p}"

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
                               lines_count: int = 50, no_states: bool = False,
                               host: str = DEFAULT_HOST, api_port: int = DEFAULT_PORT) -> str:
    """
    Инструмент 6 (P0): Чтение и вывод логов работы устройства в реальном времени.
    
    Параметры:
      - configuration: имя конфигурации устройства на сервере ESPHome API
      - port: "OTA" (по умолчанию) или serial-порт (/dev/ttyUSB0, COM3 и т.д.)
      - duration_seconds: продолжительность сбора логов в секундах (1-60, по умолчанию 10)
      - lines_count: максимальное количество собираемых строк (по умолчанию 50)
      - no_states: подавление событий изменения состояний для снижения шума (по умолчанию False)
      - host: хост сервера ESPHome
      - api_port: сетевой порт WebSocket API ESPHome
    """
    config_name = resolve_configuration(configuration)
    duration_seconds = max(1, min(60, duration_seconds))
    lines_count = max(1, min(500, lines_count))
    
    url = get_ws_url(host, api_port)
    msg_id = "logs_stream_1"
    output_log = []
    
    logger.info(f"Подключение к {url} для чтения логов {config_name} (порт: {port}, время: {duration_seconds}s, no_states={no_states})...")
    
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."
            
            logs_args = {"configuration": config_name, "port": port}
            if no_states:
                logs_args["no_states"] = True

            await ws.send(json.dumps({
                "command": "devices/logs",
                "message_id": msg_id,
                "args": logs_args
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
    comment: str = "",
    ssid: str = "",
    psk: str = "",
    config_only: bool = True,
    overwrite: bool = True,
    allow_wipe: bool = False,
    expected: str = "",
    component_id: str = "",
    fields: dict | None = None,
    sub_entities: list | dict | None = None,
    apply: bool = False,
    project_name: str = "",
    ignore_device: bool = True,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 9 (P1/P2): Управление конфигурацией устройств ESPHome через API.

    Параметр action поддерживает следующие действия:
      - "get"           — прочитать содержимое YAML-конфигурации устройства
      - "update"        — записать новое содержимое YAML-конфигурации (требует content, опционально expected для CAS)
      - "create"        — создать новую конфигурацию устройства:
                          - по готовому YAML (параметр content)
                          - или по шаблону платы (параметр board_id, опционально friendly_name, ssid, psk)
      - "rename"        — переименовать конфигурацию (требует new_name):
                          - config_only=True (по умолчанию) — офлайн переименование файлов на диске
                          - config_only=False — онлайн двухшаговый процесс (сборка -> OTA-прошивка -> переименование)
      - "delete"        — удалить конфигурацию и связанные файлы
      - "key"           — получить единый ключ шифрования Native API / OTA для устройства (devices/get_encryption_key)
      - "add_component" — прямое добавление компонента в конфигурацию устройства (devices/add_component):
                          - apply=False (по умолчанию) — режим предпросмотра (dry-run) без сохранения на диск
                          - apply=True — атомарное добавление и сохранение в файл на диске
      - "metadata"      — обновление метаданных sidecar JSON устройства без изменения YAML (devices/update):
                          поддерживает friendly_name, comment, board_id
      - "import"        — импорт/принятие обнаруженного устройства в реестр ESPHome (devices/import)
      - "ignore"        — скрытие или восстановление видимости устройства в дашборде (devices/ignore)

    Параметры:
      - configuration:  имя файла ("test.yaml") или относительный путь ("config/test.yaml")
      - content:        содержимое YAML для действий "update" или "create"
      - new_name:       новое имя для действия "rename"
      - board_id:       ID платы для создания по шаблону (например "esp32dev", "d1_mini", "nodemcuv2") или для "metadata"
      - friendly_name:  понятное имя устройства (например "Датчик климата")
      - comment:        пользовательский комментарий/заметка к устройству для действия "metadata"
      - ssid:           имя Wi-Fi сети (сохраняется в secrets.yaml с использованием !secret)
      - psk:            пароль Wi-Fi сети
      - config_only:    переименование только файлов конфигурации (True) или с онлайн OTA-прошивкой (False)
      - overwrite:      перезаписывать ли существующий файл при создании
      - allow_wipe:     разрешить перезапись при действии "update"
      - expected:       ожидаемое текущее содержимое файла (Compare-and-Swap защита при действии "update")
      - component_id:   идентификатор компонента для "add_component" (например "sensor.bme280_i2c", "wifi")
      - fields:         словарь полей компонента для "add_component"
      - sub_entities:   вложенные сущности компонента для "add_component"
      - apply:          применить ли добавление компонента на диск (для "add_component")
      - project_name:   имя проекта для действия "import"
      - ignore_device:  флаг скрытия устройства (True — скрыть, False — восстановить видимость) для действия "ignore"
      - host:           хост сервера ESPHome
      - port:           сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "manage_cfg_1"

    command_map = {
        "get":           "devices/get_config",
        "update":        "devices/update_config",
        "create":        "devices/create",
        "rename":        "devices/rename",
        "delete":        "devices/delete",
        "key":           "devices/get_encryption_key",
        "add_component": "devices/add_component",
        "metadata":      "devices/update",
        "import":        "devices/import",
        "ignore":        "devices/ignore",
    }

    action = action.strip().lower()
    if action not in command_map:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: get, update, create, rename, delete, key, add_component, metadata, import, ignore."

    command = command_map[action]
    config_name = resolve_configuration(configuration)

    # Формируем аргументы по действию
    if action == "get":
        args = {"configuration": config_name}
    elif action == "update":
        if not content:
            return "Ошибка: для действия 'update' необходимо указать параметр content."
        args = {"configuration": config_name, "content": content, "allow_wipe": allow_wipe}
        if expected:
            args["expected"] = expected
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
    elif action == "key":
        args = {"configuration": config_name}
    elif action == "metadata":
        args = {"configuration": config_name}
        if friendly_name:
            args["friendly_name"] = friendly_name
        if comment:
            args["comment"] = comment
        if board_id:
            args["board_id"] = board_id
    elif action == "import":
        dev_name = config_name.removesuffix(".yaml")
        args = {"name": dev_name}
        if project_name:
            args["project_name"] = project_name
    elif action == "ignore":
        dev_name = config_name.removesuffix(".yaml")
        args = {"name": dev_name, "ignore": ignore_device}
    elif action == "add_component":
        if not component_id:
            return "Ошибка: для действия 'add_component' необходимо указать параметр component_id."
        args = {
            "configuration": config_name,
            "component_id": component_id.strip()
        }
        if fields and isinstance(fields, dict):
            args["fields"] = fields
        if sub_entities is not None:
            args["sub_entities"] = sub_entities

    logger.info(f"manage_device_config: action={action!r}, configuration={config_name!r}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            # Для add_component в режиме dry-run (apply=False) сначала считываем текущий YAML
            if action == "add_component" and not apply:
                get_cfg_id = "add_comp_get_cfg_1"
                await ws.send(json.dumps({
                    "command": "devices/get_config",
                    "message_id": get_cfg_id,
                    "args": {"configuration": config_name}
                }))
                cur_cfg_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if cur_cfg_resp.get("error_code"):
                    return f"Ошибка чтения конфигурации перед добавлением компонента: {cur_cfg_resp.get('details', cur_cfg_resp)}"
                args["yaml"] = cur_cfg_resp.get("result", "")

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

                elif action == "key":
                    enc_key = (result or {}).get("key", "") if isinstance(result, dict) else str(result or "")
                    if not enc_key:
                        return f"ℹ️ Ключ шифрования (API/OTA encryption key) для устройства `{config_name}` не настроен или не найден."
                    return (
                        f"### 🔑 Ключ шифрования устройства `{config_name}`:\n\n"
                        f"```\n{enc_key}\n```\n\n"
                        f"> [!NOTE]\n"
                        f"> Данный ключ используется для безопасного подключения через ESPHome Native API и шифрования OTA-прошивки."
                    )

                elif action == "add_component":
                    updated_yaml = (result or {}).get("yaml", "") if isinstance(result, dict) else ""
                    if not apply:
                        return (
                            f"### 🔍 Предпросмотр добавления компонента `{component_id}` в `{config_name}` (Dry-run):\n\n"
                            f"```yaml\n{updated_yaml}\n```\n\n"
                            f"> [!TIP]\n"
                            f"> Для применения изменений на диске вызовите `manage_device_config(action=\"add_component\", configuration=\"{config_name}\", component_id=\"{component_id}\", apply=True)`."
                        )
                    return f"✅ Компонент `{component_id}` успешно добавлен в конфигурацию `{config_name}` и сохранен на диске."

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

                elif action == "metadata":
                    res_dict = result if isinstance(result, dict) else {}
                    r_friendly = res_dict.get("friendly_name") or friendly_name or "N/A"
                    r_comment = res_dict.get("comment") or comment or "—"
                    r_board = res_dict.get("board_id") or board_id or "—"
                    return (
                        f"### 📝 Метаданные устройства `{config_name}` успешно обновлены (devices/update):\n\n"
                        f"- **Отображаемое имя (friendly_name):** `{r_friendly}`\n"
                        f"- **Комментарий (comment):** `{r_comment}`\n"
                        f"- **ID платы (board_id):** `{r_board}`"
                    )

                elif action == "import":
                    res_dict = result if isinstance(result, dict) else {}
                    imported_cfg = res_dict.get("configuration", f"{config_name}")
                    warning = res_dict.get("warning")
                    out = [f"### 📥 Устройство успешно импортировано в реестр ESPHome (devices/import):\n\n- **Файл конфигурации:** `{imported_cfg}`"]
                    if warning:
                        out.append(f"\n- ⚠️ **Предупреждение:** {warning}")
                    return "".join(out)

                elif action == "ignore":
                    status_str = "скрыто из дашборда" if ignore_device else "восстановлено в дашборде"
                    return f"### 👁️ Устройство `{config_name}` успешно {status_str} (devices/ignore)."

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
    board_id: str = "",
    limit: int = 20,
    offset: int = 0,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 18 (P1): Поиск и исследование каталога компонентов ESPHome (>940 компонентов).

    Позволяет агенту находить компоненты, датчики, дисплеи и приводы, узнавать их зависимости
    (i2c, spi, uart), ограничения шин, ссылки на документацию, подробную схему настроек
    (config_entries) и поддерживаемые режимы GPIO.

    Параметр action поддерживает:
      - "search"     — поиск компонентов по query, категории, платформе с пагинацией
      - "get"        — подробный паспорт компонента со схемой параметров (components/get_component_bodies)
      - "categories" — список всех категорий компонентов ESPHome с количеством записей
      - "pin_modes"  — справочник поддерживаемых режимов пинов для GPIO-расширителей

    Параметры:
      - query:        строка поиска (название, чип, например "bme280", "dht", "ssd1306")
      - category:     фильтр по категории (например "sensor", "display", "light", "climate")
      - platform:     фильтр по платформе чипа (например "esp32", "rp2040")
      - component_id: точный ID компонента для action="get" (например "sensor.bme280_i2c")
      - board_id:     ID платы для контекстной фильтрации компонентов (опционально)
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
        if board_id:
            args["board_id"] = board_id

    elif action == "get":
        target_q = component_id or query
        command = "components/get_components"
        args = {"query": target_q, "limit": 10}
        if platform:
            args["platform"] = platform
        if board_id:
            args["board_id"] = board_id

    elif action == "categories":
        command = "components/get_categories"
        args = {}
        if board_id:
            args["board_id"] = board_id

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

                    # Запрашиваем расширенную схему настроек через components/get_component_bodies
                    body_entries = []
                    try:
                        body_args: dict = {"component_ids": [cid]}
                        if platform:
                            body_args["platform"] = platform
                        if board_id:
                            body_args["board_id"] = board_id

                        await ws.send(json.dumps({
                            "command": "components/get_component_bodies",
                            "message_id": "comp_bodies_1",
                            "args": body_args
                        }))
                        body_msg_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        body_msg = json.loads(body_msg_raw)
                        body_res = body_msg.get("result", {})
                        if isinstance(body_res, dict) and cid in body_res:
                            body_entries = body_res[cid].get("config_entries", [])
                    except Exception as err:
                        logger.warning(f"Не удалось получить тело компонента через get_component_bodies: {err}")

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

                    if body_entries:
                        output.append("\n**Схема параметров конфигурации (config_entries):**")
                        for entry in body_entries:
                            if not isinstance(entry, dict):
                                continue
                            e_key = entry.get("key", "")
                            e_type = entry.get("type", "")
                            e_label = entry.get("label", e_key)
                            e_desc = entry.get("description", "").strip()
                            e_def = entry.get("default_value")
                            e_adv = entry.get("advanced", False)
                            adv_str = " *(advanced)*" if e_adv else ""
                            def_str = f" [по умолчанию: `{e_def}`]" if e_def is not None else ""
                            desc_str = f" — {e_desc}" if e_desc else ""
                            output.append(f"  - `{e_key}` (*{e_type}*): **{e_label}**{def_str}{adv_str}{desc_str}")

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
    overwrite: bool = True,
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
      - key:       имя ключа секрета (для action="set", например "mqtt_broker_password")
      - value:     значение секрета (для action="set")
      - ssid:      имя сети Wi-Fi (для action="set_wifi")
      - psk:       пароль сети Wi-Fi (для action="set_wifi")
      - overwrite: разрешить перезапись существующего ключа при action="set" (False = create-if-absent, по умолчанию True)
      - host:      хост сервера ESPHome
      - port:      сетевой порт WebSocket API ESPHome
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
        args = {"key": key, "value": value, "overwrite": overwrite}
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
    preferences: dict | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 20 (P1/P2): Информация о хосте ESPHome, версиях, портах и настройках дашборда.

    Параметр action поддерживает:
      - "version"         — получение версии бэкенда Device Builder и ESPHome Core (config/version)
      - "serial_ports"    — список обнаруженных физических USB-Serial адаптеров на хосте (config/serial_ports)
      - "preferences"     — получение настроек пользователя и интерфейса дашборда (config/get_preferences)
      - "set_preferences" — частичное обновление настроек дашборда (config/set_preferences, требует preferences)
      - "all"             — полная сводка (версии + список Serial-портов + настройки)

    Параметры:
      - action:      действие ("version", "serial_ports", "preferences", "set_preferences", "all")
      - preferences: словарь настроек для "set_preferences" (theme, dashboard_view, version_history_enabled и др.)
      - host:        хост сервера ESPHome
      - port:        сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    action = action.strip().lower()

    valid_actions = ("version", "serial_ports", "ports", "preferences", "prefs", "set_preferences", "set_prefs", "all")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: version, serial_ports, preferences, set_preferences, all."

    if action in ("set_preferences", "set_prefs") and (preferences is None or not isinstance(preferences, dict)):
        return "Ошибка: для действия 'set_preferences' необходимо указать словарь параметров 'preferences' (например: {'theme': 'dark'})."

    logger.info(f"get_host_info: action={action!r}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            ver_data = {}
            ports_data = []
            pref_data = {}

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

            # Запрашиваем get_preferences
            if action in ("preferences", "prefs", "all"):
                pref_id = "host_pref_1"
                await ws.send(json.dumps({
                    "command": "config/get_preferences",
                    "message_id": pref_id,
                    "args": {}
                }))
                async for msg in ws:
                    d = json.loads(msg)
                    if d.get("message_id") == pref_id:
                        pref_data = d.get("result", {})
                        break

            # Записываем set_preferences
            if action in ("set_preferences", "set_prefs"):
                set_pref_id = "host_set_pref_1"
                await ws.send(json.dumps({
                    "command": "config/set_preferences",
                    "message_id": set_pref_id,
                    "args": preferences or {}
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка сохранения настроек дашборда: {data.get('details', data)}"
                updated_pref = data.get("result", {}) or {}
                return (
                    f"### ✅ Настройки дашборда ESPHome успешно обновлены (config/set_preferences):\n\n"
                    f"```json\n{json.dumps(updated_pref, indent=2, ensure_ascii=False)}\n```"
                )

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

            if action in ("preferences", "prefs", "all"):
                if output:
                    output.append("\n---\n")
                output.append("### ⚙️ Настройки интерфейса дашборда (config/get_preferences):\n")
                theme = pref_data.get("theme", "default")
                dash_view = pref_data.get("dashboard_view", "cards")
                exp_lvl = pref_data.get("experience_level", "N/A")
                ver_hist = pref_data.get("version_history_enabled", True)
                rem_comp = pref_data.get("remote_compute_only", False)
                dev_layout = pref_data.get("device_editor_layout", "yaml")
                sec_layout = pref_data.get("secrets_editor_layout", "visual")

                output.append(f"- **Тема оформления:** `{theme}`")
                output.append(f"- **Режим отображения дашборда:** `{dash_view}`")
                output.append(f"- **Уровень опыта (experience_level):** `{exp_lvl}`")
                output.append(f"- **Автоматическая история версий Git:** `{'Включена' if ver_hist else 'Отключена'}`")
                output.append(f"- **Только удаленные вычисления (remote_compute_only):** `{rem_comp}`")
                output.append(f"- **Раскладка редактора устройств:** `{dev_layout}`")
                output.append(f"- **Раскладка редактора секретов:** `{sec_layout}`")

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
      - "list"           — список задач (с фильтрацией по status_filter и/или configuration)
      - "get"            — получить полную информацию о задаче (требует job_id)
      - "cancel"         — отменить задачу (требует job_id)
      - "clean"          — очистить каталог сборки конкретного устройства (требует configuration)
      - "reset_env"      — глобальный сброс .esphome/ (PlatformIO, external_components, build)
      - "clear"          — очистить историю завершенных задач из памяти (опционально status_filter)
      - "clear_queued"   — отменить отложенное обновление устройства (deferred install, требует configuration)
      - "analyze_memory" — компиляция и анализ расхода памяти RAM и Flash устройства (firmware/analyze_memory, требует configuration)

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

    valid_actions = ("list", "get", "cancel", "clean", "reset_env", "clear", "clear_queued", "analyze_memory")
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
    if action == "analyze_memory" and not configuration:
        return "Ошибка: для действия 'analyze_memory' необходимо указать параметр configuration."

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
    elif action == "analyze_memory":
        command = "firmware/analyze_memory"
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

            # clean, reset_env и analyze_memory запускают Job — нужно ждать Job-result
            if action in ("clean", "reset_env", "analyze_memory"):
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
                        if action == "analyze_memory":
                            tail = "\n".join(follow_log[-60:])
                            return f"### 📊 Анализ расхода памяти для `{config_clean}` ({status_str}):\n\n```text\n{tail}\n```"
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
    Поддерживает нативный ESPHome WebSocket API (version_history/*) с автоматическим
    fallback на локальный Git при работе в репозитории разработки.

    Параметр action поддерживает:
      - "log"      — история коммитов по файлу или всему каталогу (sha, дата, автор, сообщение)
      - "diff"     — просмотр изменений (рабочая копия vs HEAD, либо между sha и sha_compare)
      - "show"     — чтение содержимого файла конфигурации на момент коммита sha
      - "deleted"  — поиск удаленных ранее файлов конфигураций в истории Git
      - "restore"  — откат/восстановление файла до состояния коммита sha

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
    if action == "restore" and not cfg_clean:
        return "Ошибка: для действия 'restore' необходимо указать параметр 'configuration' (YAML-файл устройства)."

    logger.info(f"manage_version_history: action={action!r}, configuration={cfg_clean!r}, sha={sha!r}")

    # 1. Попытка выполнения через нативный WebSocket API ESPHome
    ws_url = get_ws_url(host, port)
    msg_id = "ver_hist_1"
    ws_success = False
    ws_result_text = None

    try:
        async with websockets.connect(ws_url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if not first_msg.get("requires_auth"):
                if action in ("log", "list"):
                    args = {"configuration": cfg_clean} if cfg_clean else {}
                    await ws.send(json.dumps({"command": "version_history/list_versions", "message_id": msg_id, "args": args}))
                    data = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    if not data.get("error_code"):
                        versions = data.get("result", []) or []
                        if versions:
                            target_header = f"конфигурации `{cfg_clean}`" if cfg_clean else "всех конфигураций ESPHome"
                            output = [f"### 📜 История версий {target_header} (через ESPHome API, {len(versions)} коммитов):\n"]
                            for v in versions[:max_count]:
                                v_sha = v.get("short_sha") or v.get("sha", "")[:7]
                                v_author = v.get("author", "Unknown")
                                v_time = v.get("timestamp") or v.get("date", "")
                                v_msg = v.get("message", "").strip()
                                output.append(f"- `{v_sha}` | **{v_time}** | {v_author} — *{v_msg}*")
                            output.append("\n💡 Для просмотра изменений используйте `manage_version_history action=diff sha=<хэш>`")
                            ws_result_text = "\n".join(output)
                            ws_success = True

                elif action == "diff" and cfg_clean and sha:
                    await ws.send(json.dumps({"command": "version_history/get_diff", "message_id": msg_id, "args": {"configuration": cfg_clean, "sha": sha}}))
                    data = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    if not data.get("error_code") and data.get("result"):
                        diff_text = data.get("result", {}).get("diff", "").strip()
                        if diff_text:
                            ws_result_text = f"### 🔀 Различия (diff) для `{cfg_clean}` (ESPHome API, ревизия {sha}):\n\n```diff\n{diff_text}\n```"
                            ws_success = True

                elif action in ("show", "get") and cfg_clean and sha:
                    await ws.send(json.dumps({"command": "version_history/get_version", "message_id": msg_id, "args": {"configuration": cfg_clean, "sha": sha}}))
                    data = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    if not data.get("error_code") and data.get("result"):
                        content = data.get("result", {}).get("content", "")
                        ws_result_text = f"### 📄 Содержимое `{cfg_clean}` на момент коммита `{sha}` (ESPHome API):\n\n```yaml\n{content}\n```"
                        ws_success = True

                elif action in ("deleted", "list_deleted"):
                    await ws.send(json.dumps({"command": "version_history/list_deleted", "message_id": msg_id, "args": {}}))
                    data = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    if not data.get("error_code") and data.get("result"):
                        del_list = data.get("result", []) or []
                        if del_list:
                            output = [f"### 🗑 Удаленные конфигурации в истории ESPHome API ({len(del_list)} шт.):\n"]
                            for item in del_list:
                                item_cfg = item.get("configuration") if isinstance(item, dict) else str(item)
                                output.append(f"- `{item_cfg}`")
                            output.append("\n💡 Для восстановления удаленного файла используйте `manage_version_history action=\"restore\" configuration=\"<имя_файла>\"`")
                            ws_result_text = "\n".join(output)
                            ws_success = True

                elif action == "restore" and cfg_clean:
                    r_args = {"configuration": cfg_clean}
                    if sha.strip():
                        r_args["sha"] = sha.strip()
                    await ws.send(json.dumps({"command": "version_history/restore", "message_id": msg_id, "args": r_args}))
                    data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                    if not data.get("error_code") and data.get("result"):
                        restored_from = data.get("result", {}).get("restored_from", sha or "latest")
                        ws_result_text = f"### 🔄 Восстановление конфигурации `{cfg_clean}`:\n\n✅ Файл `{cfg_clean}` успешно восстановлен до состояния коммита `{restored_from}` через нативный API ESPHome!"
                        ws_success = True
    except Exception as e:
        logger.debug(f"Нативный version_history API недоступен или вернул ошибку ({e}), переключение на Git fallback")

    if ws_success and ws_result_text:
        return ws_result_text

    # 2. Fallback на локальный Git-репозиторий
    repo_info = find_git_repo(config_dir)
    if not repo_info:
        if ws_result_text:
            return ws_result_text
        return "Внимание: история версий пуста в API ESPHome (или отключена сервером), а локальный Git-репозиторий с конфигурациями не обнаружен."

    repo_root, prefix = repo_info
    rel_file = f"{prefix}/{cfg_clean}".lstrip("/") if cfg_clean else ""

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
            apply_res = await manage_device_config(
                action="update",
                configuration=cfg_clean,
                content=restored_content,
                host=host,
                port=port
            )

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


@mcp.tool()
async def manage_automations(
    action: str = "parse",
    configuration: str = "",
    component_id: str = "",
    trigger: str = "",
    kind: str = "component_on",
    automation: dict | None = None,
    expected: str | None = None,
    apply: bool = False,
    query: str = "",
    platform: str = "",
    board_id: str = "",
    refs: list | dict | str | None = None,
    include_advanced: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 25 (P2): Управление AST автоматизаций ESPHome (визуальный редактор логики).

    Позволяет агенту инспектировать, парсить, валидировать, создавать, модифицировать
    и удалять блоки автоматизаций (on_press, on_turn_on, on_state, interval, script и др.)
    с атомарной точечной вставкой в YAML-файл через нативный движок AST ESPHome.

    Параметр action поддерживает:
      - "parse"         — разбор и вывод всех автоматизаций устройства в виде AST дерева (automations/parse).
                          Требует: configuration.
      - "available"     — список доступных сущностей, триггеров и действий для устройства (automations/get_available).
                          Требует: configuration.
      - "triggers"      — глобальный каталог доступных триггеров ESPHome (automations/get_triggers).
                          Опционально: query (фильтр по имени/домену).
      - "actions"       — глобальный каталог доступных действий ESPHome (automations/get_actions).
                          Опционально: query (фильтр по имени/домену).
      - "conditions"    — глобальный каталог доступных условий ESPHome (automations/get_conditions).
                          Опционально: query (фильтр по имени/домену).
      - "light_effects" — каталог световых эффектов ESPHome (automations/get_light_effects).
                          Опционально: platform, board_id, query.
      - "filters"       — каталог фильтров для сенсоров/бинарных сенсоров (automations/get_filters).
                          Опционально: query.
      - "docs"          — паспорта и схемы параметров триггеров, действий, условий и эффектов (automations/get_bodies).
                          Требует: refs (список объектов до 50 шт, например [{'type': 'actions', 'id': 'switch.toggle'}]).
                          Опционально: include_advanced (bool).
      - "upsert"        — точечное добавление или обновление блока автоматизации в YAML-файле (automations/upsert).
                          Требует: configuration, component_id, trigger, automation (структура триггера и действий).
                          Опционально: kind ("component_on" по умолчанию), expected (ожидаемый YAML-блок), apply (True для автоматического сохранения).
      - "delete"        — точечное удаление блока автоматизации из YAML-файла (automations/delete).
                          Требует: configuration, component_id, trigger.
                          Опционально: kind ("component_on" по умолчанию), expected (ожидаемый YAML-блок), apply (True для автоматического сохранения).

    Параметры:
      - action:           режим работы ("parse", "available", "triggers", "actions", "conditions", "light_effects", "filters", "docs", "upsert", "delete")
      - configuration:    имя YAML-файла устройства (например "test.yaml")
      - component_id:     ID сущности/компонента (например "relay_switch", "test_button")
      - trigger:          имя триггера (например "on_turn_on", "on_press", "on_value")
      - kind:             тип обработчика ("component_on" по умолчанию, "interval", "script" и др.)
      - automation:       словарь описания автоматизации (trigger_id, actions, trigger_params)
      - expected:         ожидаемое текущее содержимое блока в YAML (для оптимистичной блокировки, опционально)
      - apply:            флаг сохранения изменений в файл на диск (save: true при True, иначе dry-run)
      - query:            поисковый фильтр для каталогов
      - platform:         платформа для фильтрации световых эффектов (например "esp32")
      - board_id:         ID платы для фильтрации световых эффектов
      - refs:             список ссылок на паспорта автоматизаций для action="docs" ([{'type': '...', 'id': '...'}])
      - include_advanced: выводить ли расширенные/скрытые поля в схеме паспортов
      - host:             хост сервера ESPHome
      - port:             сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "automations_tool_1"
    action = action.strip().lower()

    valid_actions = ("parse", "available", "triggers", "actions", "conditions", "light_effects", "filters", "docs", "bodies", "upsert", "delete")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: parse, available, triggers, actions, conditions, light_effects, filters, docs, upsert, delete."

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

    logger.info(f"manage_automations: action={action!r}, configuration={cfg_clean!r}, component_id={component_id!r}, apply={apply}")

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

            elif action == "light_effects":
                le_args: dict = {}
                if platform.strip():
                    le_args["platform"] = platform.strip()
                if board_id.strip():
                    le_args["board_id"] = board_id.strip()
                await ws.send(json.dumps({"command": "automations/get_light_effects", "message_id": msg_id, "args": le_args}))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка получения световых эффектов: {data.get('details', data)}"
                items = data.get("result", []) or []
                if query:
                    q = query.strip().lower()
                    items = [it for it in items if q in it.get("id", "").lower() or q in it.get("name", "").lower() or any(q in p.lower() for p in it.get("applies_to", []))]

                output = [f"### 💡 Каталог световых эффектов ESPHome ({len(items)} шт.):\n"]
                for it in items[:50]:
                    e_id = it.get("id", "")
                    name = it.get("name", "")
                    applies = ", ".join(f"`{p}`" for p in it.get("applies_to", []))
                    output.append(f"- **`{e_id}`** ({name}) — платформы: {applies}")
                return "\n".join(output)

            elif action == "filters":
                await ws.send(json.dumps({"command": "automations/get_filters", "message_id": msg_id, "args": {}}))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка получения фильтров: {data.get('details', data)}"
                items = data.get("result", []) or []
                if query:
                    q = query.strip().lower()
                    items = [it for it in items if q in it.get("id", "").lower() or q in it.get("name", "").lower() or any(q in d.lower() for d in it.get("applies_to", []))]

                output = [f"### 🔍 Каталог фильтров сенсоров ESPHome ({len(items)} шт.):\n"]
                for it in items[:50]:
                    f_id = it.get("id", "")
                    name = it.get("name", "")
                    applies = ", ".join(f"`{d}`" for d in it.get("applies_to", []))
                    vtype = it.get("value_type", "")
                    v_str = f" [тип: `{vtype}`]" if vtype else ""
                    output.append(f"- **`{f_id}`** ({name}){v_str} — домены: {applies}")
                return "\n".join(output)

            elif action in ("docs", "bodies"):
                refs_list = []
                if isinstance(refs, str) and refs.strip():
                    try:
                        parsed_refs = json.loads(refs)
                        refs_list = parsed_refs if isinstance(parsed_refs, list) else [parsed_refs]
                    except json.JSONDecodeError:
                        return "Ошибка: параметр 'refs' должен быть валидным JSON списком объектов [{'type': '...', 'id': '...'}]"
                elif isinstance(refs, list):
                    refs_list = refs
                elif isinstance(refs, dict):
                    refs_list = [refs]

                if not refs_list:
                    return (
                        "Ошибка: для действия 'docs' необходимо указать параметр 'refs' "
                        "(список объектов до 50 шт, например [{'type': 'actions', 'id': 'switch.toggle'}])."
                    )

                body_args = {
                    "refs": refs_list[:50],
                    "include_advanced": include_advanced
                }
                await ws.send(json.dumps({"command": "automations/get_bodies", "message_id": msg_id, "args": body_args}))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка получения паспортов автоматизаций: {data.get('details', data)}"

                bodies_dict = data.get("result", {}) or {}
                if not bodies_dict:
                    return "Паспорта по указанным ссылкам не найдены."

                output = [f"### 📚 Паспорта элементов автоматизаций ({len(bodies_dict)} шт.):\n"]
                for ref_key, body in bodies_dict.items():
                    if not isinstance(body, dict):
                        continue
                    b_id = body.get("id", ref_key)
                    b_name = body.get("name", b_id)
                    b_desc = body.get("description", "").strip()
                    docs_url = body.get("docs_url", "")
                    entries = body.get("config_entries", [])

                    output.append(f"#### 🔹 {b_name} (`{b_id}`):")
                    if b_desc:
                        output.append(f"{b_desc}\n")
                    if docs_url:
                        output.append(f"- **Документация:** {docs_url}")

                    if entries:
                        output.append("- **Параметры конфигурации (config_entries):**")
                        for entry in entries:
                            if not isinstance(entry, dict):
                                continue
                            ekey = entry.get("key", "")
                            etype = entry.get("type", "")
                            elabel = entry.get("label", ekey)
                            req = entry.get("required", False)
                            def_val = entry.get("default_value")
                            req_str = " *(обязательный)*" if req else ""
                            def_str = f" [дефолт: `{def_val}`]" if def_val is not None else ""
                            output.append(f"  - `{ekey}` (*{etype}*): **{elabel}**{req_str}{def_str}")
                    output.append("")

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
                args_dict = {
                    "configuration": cfg_clean,
                    "location": location_dict,
                    "automation": automation,
                    "save": bool(apply)
                }
                if expected is not None:
                    args_dict["expected"] = expected

                await ws.send(json.dumps({
                    "command": "automations/upsert",
                    "message_id": msg_id,
                    "args": args_dict
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

                return (
                    f"### ⚡ Автоматизация успешно добавлена в `{cfg_clean}`!\n\n"
                    f"- **Компонент:** `{component_id}` | **Триггер:** `{trigger}`\n"
                    f"- **Целевые строки:** {diff.get('fromLine')}-{diff.get('toLine')}\n"
                    f"- **Вставленный YAML блок:**\n```yaml\n{diff_rep}\n```"
                )

            elif action == "delete":
                location_dict = {
                    "component_id": component_id.strip(),
                    "trigger": trigger.strip(),
                    "kind": kind.strip() or "component_on"
                }
                args_dict = {
                    "configuration": cfg_clean,
                    "location": location_dict,
                    "save": bool(apply)
                }
                if expected is not None:
                    args_dict["expected"] = expected

                await ws.send(json.dumps({
                    "command": "automations/delete",
                    "message_id": msg_id,
                    "args": args_dict
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

                return f"### 🗑 Автоматизация `{trigger}` компонента `{component_id}` успешно удалена из `{cfg_clean}`!"

    except Exception as e:
        error_msg = f"Критическая ошибка manage_automations ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def get_firmware_binaries(
    configuration: str,
    action: str = "list",
    file: str = "",
    save_path: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 26 (P2): Экспорт и скачивание скомпилированных артефактов прошивки (firmware/*).

    Позволяет получать список готовых бинарных артефактов (factory.bin, ota.bin, firmware.elf),
    генерировать временные одноразовые токены и скачивать бинарники на диск хоста.

    Параметр action поддерживает:
      - "list"     — получение списка всех доступных скомпилированных артефактов на диске (firmware/get_binaries).
      - "token"    — генерация одноразового токена и ссылки для скачивания конкретного файла (firmware/download_token).
                     Требует: file (например 'firmware.factory.bin' или 'firmware.ota.bin').
      - "download" — скачивание бинарного файла через HTTP эндпоинт (/api/firmware/download) и сохранение в save_path.
                     Требует: file. Если save_path не указан, файл сохраняется в текущую директорию под оригинальным именем.

    Параметры:
      - configuration: имя YAML-файла устройства (например "test.yaml")
      - action:        режим работы ("list", "token", "download", по умолчанию "list")
      - file:          имя целевого файла артефакта (например "firmware.factory.bin", "firmware.ota.bin", "firmware.elf")
      - save_path:     локальный путь для сохранения бинарника при action="download" (опционально)
      - host:          хост сервера ESPHome
      - port:          сетевой порт WebSocket/HTTP API ESPHome
    """
    if not configuration or not configuration.strip():
        return "Ошибка: параметр 'configuration' обязателен для работы с артефактами прошивки."

    action = action.strip().lower()
    valid_actions = ("list", "token", "download")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: list, token, download."

    if action in ("token", "download") and not file.strip():
        return f"Ошибка: для действия '{action}' необходимо указать параметр 'file' (например 'firmware.factory.bin' или 'firmware.ota.bin')."

    cfg_clean = resolve_configuration(configuration)
    url = get_ws_url(host, port)
    http_base = get_http_url(host, port)
    msg_id = "binaries_tool_1"

    logger.info(f"get_firmware_binaries: action={action!r}, configuration={cfg_clean!r}, file={file!r}")

    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            if action == "list":
                await ws.send(json.dumps({
                    "command": "firmware/get_binaries",
                    "message_id": msg_id,
                    "args": {"configuration": cfg_clean}
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка получения списка артефактов ({cfg_clean}): {data.get('details', data)}"

                items = data.get("result", []) or []
                if not items:
                    return f"📦 Для устройства `{cfg_clean}` не найдено скомпилированных бинарных файлов на диске. Сначала выполните компиляцию (`compile_firmware`)."

                output = [f"### 📦 Скомпилированные артефакты прошивки для `{cfg_clean}` ({len(items)} шт.):\n"]
                for it in items:
                    f_name = it.get("file", "")
                    title = it.get("title", "")
                    b_type = it.get("type", "bin")
                    desc = it.get("description", "")
                    output.append(f"- **`{f_name}`** (тип: `{b_type}`): **{title}**")
                    if desc:
                        output.append(f"  *{desc}*")

                output.append("\n💡 Для скачивания файла используйте `action='token'` (получение ссылки) или `action='download'` (сохранение на диск).")
                return "\n".join(output)

            elif action in ("token", "download"):
                target_file = file.strip()
                await ws.send(json.dumps({
                    "command": "firmware/download_token",
                    "message_id": msg_id,
                    "args": {"configuration": cfg_clean, "file": target_file}
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка выпуска токена для скачивания `{target_file}` ({cfg_clean}): {data.get('details', data)}"

                res = data.get("result", {}) or {}
                token = res.get("token", "")
                filename = res.get("filename") or target_file
                if not token:
                    return f"Ошибка: сервер не вернул токен для скачивания `{target_file}`."

                download_url = f"{http_base}/api/firmware/download?token={token}"

                if action == "token":
                    return (
                        f"### 🔑 Токен и ссылка для скачивания `{filename}`:\n\n"
                        f"- **Имя файла:** `{filename}`\n"
                        f"- **Токен авторизации:** `{token}`\n"
                        f"- **Ссылка для скачивания (HTTP GET):**\n"
                        f"  [{download_url}]({download_url})\n\n"
                        f"⏱ *Токен одноразовый и действителен в течение ~60 секунд.*"
                    )

                elif action == "download":
                    # Выполняем HTTP GET загрузку
                    dest_path = save_path.strip() if save_path.strip() else os.path.join(os.getcwd(), filename)
                    dest_dir = os.path.dirname(os.path.abspath(dest_path))
                    if dest_dir and not os.path.exists(dest_dir):
                        os.makedirs(dest_dir, exist_ok=True)

                    req = urllib.request.Request(download_url)
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        binary_data = resp.read()

                    with open(dest_path, "wb") as f:
                        f.write(binary_data)

                    file_size = len(binary_data)
                    sha256_hash = hashlib.sha256(binary_data).hexdigest()

                    size_str = f"{file_size:,} байт"
                    if file_size > 1024 * 1024:
                        size_str += f" ({file_size / (1024 * 1024):.2f} МБ)"
                    elif file_size > 1024:
                        size_str += f" ({file_size / 1024:.2f} КБ)"

                    return (
                        f"### 📥 Артефакт прошивки успешно скачан!\n\n"
                        f"- **Файл:** `{filename}`\n"
                        f"- **Сохранен в:** `{os.path.abspath(dest_path)}`\n"
                        f"- **Размер:** {size_str}\n"
                        f"- **SHA-256:** `{sha256_hash}`"
                    )

    except Exception as e:
        error_msg = f"Критическая ошибка get_firmware_binaries ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def manage_remote_build(
    action: str = "get_settings",
    enabled: bool | None = None,
    cleanup_ttl_seconds: int | None = None,
    hostname: str = "",
    target_port: int = 6052,
    pairing_key: str = "",
    peer_id: str = "",
    dashboard_id: str = "",
    pin_sha256: str = "",
    receiver_label: str = "",
    offloader_label: str = "",
    open_window: bool = True,
    version_match_policy: str = "",
    include_local_in_pool: bool | None = None,
    remote_builds_enabled: bool | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 27 (P2): Управление распределенной кластерной компиляцией (Remote Build / Offloading).

    Позволяет агенту мониторить состояние распределенной сборки, настраивать параметры offloading,
    получать identity узла, управлять окном сопряжения, выполнять сопряжение с удаленными узлами сборки (handshake Noise XX / fingerprint),
    ротировать ключи, одобрять, разрывать сопряжение, включать/выключать пиры и удалять их.

    Параметр action поддерживает:
      - "get_settings"           — получение текущих настроек кластерной компиляции и списка подключенных пиров (remote_build/get_settings).
      - "set_settings"           — обновление настроек (enabled, cleanup_ttl_seconds) (remote_build/set_settings).
      - "get_identity"           — получение криптографической идентичности данного узла сборки (remote_build/get_identity).
      - "rotate_identity"        — принудительная ротация X25519 пары ключей узла (remote_build/rotate_identity).
      - "set_pairing_window"     — открытие/закрытие окна сопряжения Noise XX (remote_build/set_pairing_window, параметр open_window).
      - "set_offloader_settings" — настройка параметров удаленного планировщика сборки (remote_build/set_offloader_settings, параметры remote_builds_enabled, version_match_policy, include_local_in_pool).
      - "set_pairing_enabled"    — включение/выключение автоматической отправки сборок на узел (remote_build/set_pairing_enabled, параметры pin_sha256/peer_id, enabled).
      - "preview_pair"           — предварительная проверка и получение fingerprint (pin_sha256) удаленного узла (remote_build/preview_pair).
                                   Требует: hostname, опционально: target_port (по умолчанию 6052).
      - "request_pair"           — запрос сопряжения с удаленным узлом компиляции (remote_build/request_pair).
                                   Требует: hostname. Опционально: target_port, pin_sha256 (автоопределяется через preview_pair),
                                   receiver_label, offloader_label, pairing_key.
      - "approve_peer"           — одобрение входящего запроса на сопряжение от пира (remote_build/approve_peer).
                                   Требует: dashboard_id (или peer_id).
      - "unpair"                 — разрыв сопряжения с удаленным узлом (remote_build/unpair).
                                   Требует: pin_sha256 (или peer_id).
      - "remove_peer"            — удаление пира из конфигурации билдера (remote_build/remove_peer).
                                   Требует: dashboard_id (или peer_id).

    Параметры:
      - action:                режим работы ("get_settings", "set_settings", "get_identity", "rotate_identity", "set_pairing_window", "set_offloader_settings", "set_pairing_enabled", "preview_pair", "request_pair", "approve_peer", "unpair", "remove_peer")
      - enabled:               включение/отключение кластерной компиляции или флага активности пира (bool)
      - cleanup_ttl_seconds:   время хранения артефактов в секундах при set_settings (int)
      - hostname:              хост/IP удаленного узла компиляции для сопряжения
      - target_port:           сетевой порт удаленного узла компиляции (по умолчанию 6052)
      - pairing_key:           ключ авторизации для сопряжения (если требуется удаленным сервером)
      - peer_id:               алиас для dashboard_id или pin_sha256 (для обратной совместимости)
      - dashboard_id:          идентификатор дашборда пира (для approve_peer и remove_peer)
      - pin_sha256:            хэш публичного ключа (fingerprint) удаленного узла (для request_pair, unpair, set_pairing_enabled)
      - receiver_label:        понятное имя удаленного узла (по умолчанию hostname)
      - offloader_label:       имя клиента сопряжения (по умолчанию "ESPHome MCP Server")
      - open_window:           открыть (True) или закрыть (False) окно сопряжения (для set_pairing_window)
      - version_match_policy:  политика сверки версий ("any", "release", "exact", "exact_required" для set_offloader_settings)
      - include_local_in_pool: включать ли локальную машину в пул параллельных сборок при нагрузке (bool для set_offloader_settings)
      - remote_builds_enabled: мастер-переключатель использования удаленной сборки (bool для set_offloader_settings)
      - host:                  хост сервера ESPHome
      - port:                  сетевой порт WebSocket API ESPHome
    """
    url = get_ws_url(host, port)
    msg_id = "remote_build_tool_1"
    action = action.strip().lower()

    valid_actions = (
        "get_settings", "set_settings", "get_identity", "rotate_identity",
        "set_pairing_window", "set_offloader_settings", "set_pairing_enabled",
        "preview_pair", "request_pair", "approve_peer", "unpair", "remove_peer"
    )
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: {', '.join(valid_actions)}."

    if action in ("preview_pair", "request_pair") and not hostname.strip():
        return f"Ошибка: для действия '{action}' необходимо указать параметр 'hostname' (хост или IP-адрес удаленного билдера)."

    effective_dash_id = (dashboard_id or peer_id).strip()
    if action in ("approve_peer", "remove_peer") and not effective_dash_id:
        return f"Ошибка: для действия '{action}' необходимо указать параметр 'peer_id' (или 'dashboard_id')."

    effective_pin = (pin_sha256 or peer_id).strip()
    if action in ("unpair", "set_pairing_enabled") and not effective_pin:
        return f"Ошибка: для действия '{action}' необходимо указать параметр 'peer_id' (или 'pin_sha256')."

    logger.info(f"manage_remote_build: action={action!r}, hostname={hostname!r}, dashboard_id={effective_dash_id!r}, pin_sha256={effective_pin!r}")

    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."

            if action == "get_settings":
                await ws.send(json.dumps({
                    "command": "remote_build/get_settings",
                    "message_id": msg_id,
                    "args": {}
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка получения настроек remote_build: {data.get('details', data)}"

                res = data.get("result", {}) or {}
                is_enabled = res.get("enabled", False)
                ttl = res.get("cleanup_ttl_seconds", 0)
                peers = res.get("peers", []) or []

                status_emoji = "🟢 Включена (enabled=True)" if is_enabled else "🔴 Отключена (enabled=False)"
                ttl_hours = round(ttl / 3600, 1) if ttl else 0

                output = [
                    "### 🏗 Настройки кластерной компиляции ESPHome (Remote Build):\n",
                    f"- **Статус кластера:** `{status_emoji}`",
                    f"- **TTL очистки сборок:** `{ttl:,} сек` (~{ttl_hours} ч)",
                    f"- **Подключенные узлы / пиры ({len(peers)} шт.):**"
                ]

                if not peers:
                    output.append("  *(список узлов пуст, сопряженные билдеры отсутствуют)*")
                else:
                    for p in peers:
                        p_id = p.get("dashboard_id") or p.get("id") or p.get("peer_id") or "N/A"
                        p_host = p.get("hostname") or p.get("host") or "N/A"
                        p_port = p.get("port") or 6052
                        p_status = p.get("status", "unknown")
                        p_name = p.get("name") or p.get("receiver_label") or "Remote Builder"
                        output.append(f"  - **{p_name}** (`{p_id}`): `{p_host}:{p_port}` | статус: `{p_status}`")

                return "\n".join(output)

            elif action == "get_identity":
                await ws.send(json.dumps({
                    "command": "remote_build/get_identity",
                    "message_id": msg_id,
                    "args": {}
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка получения идентичности узла remote_build: {data.get('details', data)}"

                res = data.get("result", {}) or {}
                d_id = res.get("dashboard_id", "N/A")
                pin = res.get("pin_sha256", "N/A")
                srv_v = res.get("server_version", "N/A")
                esp_v = res.get("esphome_version", "N/A")
                l_bound = res.get("listener_bound", False)
                l_host = res.get("listener_host") or "N/A"
                l_port = res.get("listener_port", 6052)
                l_addrs = ", ".join(res.get("listener_addresses", [])) or "N/A"

                return (
                    f"### 🛡 Идентичность узла кластерной сборки ESPHome:\n\n"
                    f"- **Dashboard ID:** `{d_id}`\n"
                    f"- **Fingerprint (pin_sha256):** `{pin}`\n"
                    f"- **Версия сервера:** `{srv_v}` (ESPHome `{esp_v}`)\n"
                    f"- **Noise Listener:** `{'Активен' if l_bound else 'Не активен'}` (`{l_host}:{l_port}`)\n"
                    f"- **Сетевые адреса:** `{l_addrs}`"
                )

            elif action == "set_settings":
                args_dict = {}
                if enabled is not None:
                    args_dict["enabled"] = enabled
                if cleanup_ttl_seconds is not None:
                    args_dict["cleanup_ttl_seconds"] = cleanup_ttl_seconds

                if not args_dict:
                    return "Предупреждение: для set_settings не переданы параметры обновления (enabled или cleanup_ttl_seconds)."

                await ws.send(json.dumps({
                    "command": "remote_build/set_settings",
                    "message_id": msg_id,
                    "args": args_dict
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка обновления настроек remote_build: {data.get('details', data)}"

                res = data.get("result", {}) or {}
                is_enabled = res.get("enabled", False)
                ttl = res.get("cleanup_ttl_seconds", 0)

                return (
                    f"### ✅ Настройки кластерной компиляции успешно обновлены!\n\n"
                    f"- **Кластерная сборка:** `{'Включена' if is_enabled else 'Отключена'}` (enabled={is_enabled})\n"
                    f"- **TTL очистки:** `{ttl:,} сек`\n"
                )

            elif action == "preview_pair":
                t_host = hostname.strip()
                t_port = target_port if target_port else 6052
                await ws.send(json.dumps({
                    "command": "remote_build/preview_pair",
                    "message_id": msg_id,
                    "args": {"hostname": t_host, "port": t_port}
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=15.0))
                if data.get("error_code"):
                    return f"Ошибка предварительной проверки сопряжения с `{t_host}:{t_port}`: {data.get('details', data)}"

                res = data.get("result", {}) or {}
                fingerprint = res.get("pin_sha256") or res.get("fingerprint") or "N/A"
                req_key = res.get("requires_pairing_key", False)

                return (
                    f"### 🤝 Результат проверки узла для сопряжения `{t_host}:{t_port}`:\n\n"
                    f"- **Fingerprint публичного ключа (pin_sha256):** `{fingerprint}`\n"
                    f"- **Требуется pairing key:** `{'Да' if req_key else 'Нет'}`\n\n"
                    f"💡 Для завершения сопряжения вызовите `action='request_pair', hostname='{t_host}', target_port={t_port}`."
                )

            elif action == "request_pair":
                t_host = hostname.strip()
                t_port = target_port if target_port else 6052
                target_pin = pin_sha256.strip()

                # Если fingerprint не передан, запрашиваем его автоматически через preview_pair
                if not target_pin:
                    await ws.send(json.dumps({
                        "command": "remote_build/preview_pair",
                        "message_id": "pair_prev_auto",
                        "args": {"hostname": t_host, "port": t_port}
                    }))
                    prev_data = json.loads(await asyncio.wait_for(ws.recv(), timeout=15.0))
                    if prev_data.get("error_code"):
                        return f"Ошибка предварительного получения pin_sha256 с `{t_host}:{t_port}`: {prev_data.get('details', prev_data)}"
                    prev_res = prev_data.get("result", {}) or {}
                    target_pin = prev_res.get("pin_sha256") or prev_res.get("fingerprint") or ""
                    if not target_pin:
                        return f"Ошибка: удаленный узел `{t_host}:{t_port}` не вернул pin_sha256 при preview_pair."

                r_label = receiver_label.strip() or t_host
                o_label = offloader_label.strip() or "ESPHome MCP Server"

                args_dict = {
                    "hostname": t_host,
                    "port": t_port,
                    "pin_sha256": target_pin,
                    "receiver_label": r_label,
                    "offloader_label": o_label
                }
                if pairing_key.strip():
                    args_dict["pairing_key"] = pairing_key.strip()

                await ws.send(json.dumps({
                    "command": "remote_build/request_pair",
                    "message_id": msg_id,
                    "args": args_dict
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=15.0))
                if data.get("error_code"):
                    return f"Ошибка запроса сопряжения с `{t_host}:{t_port}`: {data.get('details', data)}"

                res = data.get("result", {}) or {}
                return (
                    f"### 🚀 Запрос на сопряжение с `{t_host}:{t_port}` успешно отправлен!\n\n"
                    f"- **Fingerprint (pin_sha256):** `{target_pin}`\n"
                    f"- **Имя узла:** `{r_label}`\n"
                    f"- **Ответ сервера:** `{res if res else 'OK'}`"
                )

            elif action in ("approve_peer", "remove_peer"):
                cmd_args = {"dashboard_id": effective_dash_id}
                await ws.send(json.dumps({
                    "command": f"remote_build/{action}",
                    "message_id": msg_id,
                    "args": cmd_args
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка выполнения команды '{action}' для dashboard_id `{effective_dash_id}`: {data.get('details', data)}"

                res = data.get("result", {}) or {}
                action_titles = {
                    "approve_peer": "одобрен",
                    "remove_peer": "удален из конфигурации"
                }
                return f"### ✅ Пир `{effective_dash_id}` успешно {action_titles.get(action, action)}!"

            elif action == "unpair":
                cmd_args = {"pin_sha256": effective_pin}
                await ws.send(json.dumps({
                    "command": "remote_build/unpair",
                    "message_id": msg_id,
                    "args": cmd_args
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка выполнения unpair для pin_sha256 `{effective_pin}`: {data.get('details', data)}"

                return f"### ✅ Сопряжение для pin_sha256 `{effective_pin}` успешно разорвано!"

            elif action == "set_pairing_window":
                is_open = open_window if open_window is not None else (enabled if enabled is not None else True)
                await ws.send(json.dumps({
                    "command": "remote_build/set_pairing_window",
                    "message_id": msg_id,
                    "args": {"open": is_open}
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка управления окном сопряжения: {data.get('details', data)}"
                res = data.get("result", {}) or {}
                exp_sec = res.get("expires_in_seconds")
                exp_str = f" (истекает через {exp_sec:.0f} сек)" if exp_sec is not None else ""
                status_text = "открыто" if is_open else "закрыто"
                return f"### 🪟 Окно сопряжения Noise XX успешно {status_text}{exp_str} (remote_build/set_pairing_window)."

            elif action == "rotate_identity":
                await ws.send(json.dumps({
                    "command": "remote_build/rotate_identity",
                    "message_id": msg_id,
                    "args": {}
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка ротации ключей identity: {data.get('details', data)}"
                res = data.get("result", {}) or {}
                dash_id = res.get("dashboard_id", "N/A")
                pin = res.get("pin_sha256", "N/A")
                pub_key = res.get("public_key", "N/A")
                return (
                    f"### 🔑 Ключи узла успешно ротированы (remote_build/rotate_identity):\n\n"
                    f"- **Dashboard ID:** `{dash_id}`\n"
                    f"- **Новый Fingerprint (pin_sha256):** `{pin}`\n"
                    f"- **Публичный ключ:** `{pub_key}`\n\n"
                    f"⚠️ *Ранее сопряженные пиры должны повторно подтвердить сопряжение из-за смены fingerprint.*"
                )

            elif action == "set_offloader_settings":
                offloader_args = {}
                if remote_builds_enabled is not None:
                    offloader_args["remote_builds_enabled"] = remote_builds_enabled
                elif enabled is not None:
                    offloader_args["remote_builds_enabled"] = enabled

                if version_match_policy.strip():
                    offloader_args["version_match_policy"] = version_match_policy.strip().lower()

                if include_local_in_pool is not None:
                    offloader_args["include_local_in_pool"] = include_local_in_pool

                if not offloader_args:
                    return "Ошибка: для действия 'set_offloader_settings' необходимо указать хотя бы одну настройку (remote_builds_enabled, version_match_policy или include_local_in_pool)."

                await ws.send(json.dumps({
                    "command": "remote_build/set_offloader_settings",
                    "message_id": msg_id,
                    "args": offloader_args
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка обновления настроек offloader: {data.get('details', data)}"
                res = data.get("result", {}) or {}
                return (
                    f"### ⚙️ Настройки планировщика кластерной сборки успешно обновлены (remote_build/set_offloader_settings):\n\n"
                    f"```json\n{json.dumps(res, indent=2, ensure_ascii=False)}\n```"
                )

            elif action == "set_pairing_enabled":
                is_en = enabled if enabled is not None else True
                await ws.send(json.dumps({
                    "command": "remote_build/set_pairing_enabled",
                    "message_id": msg_id,
                    "args": {"pin_sha256": effective_pin, "enabled": is_en}
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("error_code"):
                    return f"Ошибка переключения активности сопряжения для pin_sha256 `{effective_pin}`: {data.get('details', data)}"
                status_str = "включена" if is_en else "отключена"
                return f"### ✅ Автоматическая маршрутизация сборок для узла `{effective_pin}` успешно {status_str} (remote_build/set_pairing_enabled)."

    except Exception as e:
        error_msg = f"Критическая ошибка manage_remote_build ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def authenticate_esphome(
    action: str = "login",
    username: str = "",
    password: str = "",
    token: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 15 (P2): Управление аутентификацией и сессиями на ESPHome-сервере.

    Параметр action поддерживает:
      - "login"   — вход в систему по логину/паролю или токену (auth/login)
      - "logout"  — отзыв текущего токена сессии и закрытие соединения (auth/logout)
      - "refresh" — продление срока действия токена текущей сессии (auth/refresh)

    Варианты аутентификации (при action="login"):
      1. По логину и паролю: укажите username и password
      2. По токену (повторный вход): укажите только token

    Параметры:
      - action:   действие ("login", "logout", "refresh")
      - username: имя пользователя ESPHome
      - password: пароль учетной записи
      - token:    токен доступа (JWT / session token)
      - host:     хост сервера ESPHome
      - port:     сетевой порт WebSocket API ESPHome
    """
    action = action.strip().lower()
    valid_actions = ("login", "logout", "refresh")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: login, logout, refresh."

    if action == "login" and not token and not (username and password):
        return "Ошибка: для действия 'login' необходимо указать либо (username + password), либо token."

    url = get_ws_url(host, port)
    p = port if port is not None else DEFAULT_PORT
    msg_id = f"auth_{action}_1"

    logger.info(f"authenticate_esphome: action={action!r}, host={host!r}, by_token={bool(token)}")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)

            if action == "login" and not first_msg.get("requires_auth"):
                return f"ℹ️ ESPHome на {host}:{p} не требует аутентификации (requires_auth=false). Вход не нужен."

            if action == "login":
                args: dict = {"token": token} if token else {"username": username, "password": password}
                cmd = "auth/login"
            elif action == "logout":
                args = {}
                cmd = "auth/logout"
            elif action == "refresh":
                args = {"token": token} if token else {}
                cmd = "auth/refresh"

            await ws.send(json.dumps({
                "command": cmd,
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
                        return "❌ Ошибка аутентификации: сессия не авторизована или неверные учетные данные."
                    if code == "rate_limited":
                        return "❌ Слишком много попыток. Повторите через 5 минут."
                    return f"❌ Ошибка auth/{action}: {data.get('details', data)}"

                result = data.get("result", {}) or {}

                if action == "login":
                    new_token = result.get("token", "")
                    expires_at = result.get("expires_at", "")
                    return (
                        f"✅ Аутентификация успешна на {host}:{p}\n"
                        f"- **Токен:** `{new_token}`\n"
                        f"- **Действителен до:** {expires_at}\n\n"
                        f"💡 Сохраните токен для повторного входа без пароля (параметр token)."
                    )

                elif action == "logout":
                    return f"✅ Сессия успешно завершена на {host}:{p} (auth/logout): токен отозван."

                elif action == "refresh":
                    new_token = result.get("token", "")
                    expires_at = result.get("expires_at", "")
                    return (
                        f"### 🔄 Срок действия токена успешно продлен (auth/refresh):\n\n"
                        f"- **Токен:** `{new_token}`\n"
                        f"- **Новый срок действия:** {expires_at}"
                    )

    except Exception as e:
        error_msg = f"Критическая ошибка authenticate_esphome ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


# ==============================================================================
# РАЗДЕЛ: ДИНАМИЧЕСКИЕ И КОНТЕКСТНЫЕ РЕСУРСЫ MCP (MCP Resources, Фаза 4.1)
# ==============================================================================

@mcp.resource("esphome://devices", name="devices_registry", description="Актуальный реестр всех сконфигурированных устройств ESPHome и их статусов", mime_type="application/json")
async def resource_devices() -> str:
    """
    Ресурс: Список всех устройств ESPHome с их IP-адресами, статусом онлайн/офлайн и версиями.
    """
    url = get_ws_url(DEFAULT_HOST, DEFAULT_PORT)
    msg_id = "res_dev_1"
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return json.dumps({"error": "requires_auth", "message": "ESPHome требует аутентификацию"}, ensure_ascii=False)

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
                    return json.dumps({"error": data.get("error_code"), "details": data.get("details", data)}, ensure_ascii=False)

                result = data.get("result", {})
                devices = result.get("configured", result.get("devices", [])) if isinstance(result, dict) else (result if isinstance(result, list) else [])

                # Дополнительно опрашиваем devices/get_states для получения актуальных статусов
                states_map = {}
                try:
                    st_id = "res_dev_st_1"
                    await ws.send(json.dumps({"command": "devices/get_states", "message_id": st_id, "args": {}}))
                    st_data = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                    if not st_data.get("error_code"):
                        states_map = st_data.get("result", {}) or {}
                except Exception:
                    pass

                structured = []
                for dev in devices:
                    if not isinstance(dev, dict):
                        continue
                    cfg = dev.get("configuration", "")
                    name = dev.get("name", "")
                    runtime = dev.get("runtime_state", {}) or {}
                    st_val = states_map.get(cfg)
                    if isinstance(st_val, str):
                        state = st_val
                    elif isinstance(st_val, dict):
                        state = st_val.get("state", runtime.get("state", "unknown"))
                    else:
                        state = runtime.get("state") or dev.get("state", "unknown")
                    ip_list = runtime.get("ip_addresses", [])
                    structured.append({
                        "name": name,
                        "configuration": cfg,
                        "friendly_name": dev.get("friendly_name") or name,
                        "status": state,
                        "ip_addresses": ip_list,
                        "deployed_version": runtime.get("deployed_version", dev.get("current_version", "")),
                        "board": dev.get("board", runtime.get("board", "")),
                        "platform": dev.get("platform", runtime.get("platform", "")),
                        "has_pending_changes": dev.get("has_pending_changes", False),
                        "labels": dev.get("labels", [])
                    })

                return json.dumps({
                    "total": len(structured),
                    "devices": structured
                }, indent=2, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Ошибка получения ресурса esphome://devices: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.resource("esphome://configs/{configuration}", name="device_configuration", description="Исходная YAML-конфигурация устройства ESPHome", mime_type="text/yaml")
async def resource_device_config(configuration: str) -> str:
    """
    Ресурс: Получение исходной YAML-конфигурации устройства по имени файла.
    """
    cfg_name = resolve_configuration(configuration)
    url = get_ws_url(DEFAULT_HOST, DEFAULT_PORT)
    msg_id = f"res_cfg_{cfg_name}"
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return f"# Ошибка: ESPHome на {DEFAULT_HOST}:{DEFAULT_PORT} требует авторизацию."

            await ws.send(json.dumps({
                "command": "devices/get_config",
                "message_id": msg_id,
                "args": {"configuration": cfg_name}
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return f"# Ошибка получения конфигурации {cfg_name}: {data.get('details', data)}"
                result = data.get("result", "")
                return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Ошибка получения ресурса esphome://configs/{cfg_name}: {e}")
        return f"# Ошибка чтения конфигурации {cfg_name}: {str(e)}"


@mcp.resource("esphome://components/{component_id}", name="component_schema", description="Схема настроек и паспорт компонента ESPHome (config_entries, шины, зависимости)", mime_type="application/json")
async def resource_component_schema(component_id: str) -> str:
    """
    Ресурс: Паспорт и схема параметров компонента ESPHome.
    """
    cid = component_id.strip()
    url = get_ws_url(DEFAULT_HOST, DEFAULT_PORT)
    msg_id = f"res_comp_{cid}"
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return json.dumps({"error": "requires_auth", "message": "ESPHome требует аутентификацию"}, ensure_ascii=False)

            await ws.send(json.dumps({
                "command": "components/get_component_bodies",
                "message_id": msg_id,
                "args": {"component_ids": [cid]}
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return json.dumps({"error": data.get("error_code"), "details": data.get("details", data)}, ensure_ascii=False)
                res = data.get("result", {})
                if isinstance(res, dict) and cid in res:
                    return json.dumps(res[cid], indent=2, ensure_ascii=False)
                elif isinstance(res, dict) and res:
                    return json.dumps(res, indent=2, ensure_ascii=False)
                return json.dumps({"component_id": cid, "message": "Компонент не найден"}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Ошибка получения ресурса esphome://components/{cid}: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.resource("esphome://boards/{board_id}", name="board_pinout", description="Паспорт платы микроконтроллера и распиновка GPIO (pinout)", mime_type="application/json")
async def resource_board_pinout(board_id: str) -> str:
    """
    Ресурс: Паспорт платы и аппаратная распиновка пинов.
    """
    bid = board_id.strip()
    url = get_ws_url(DEFAULT_HOST, DEFAULT_PORT)
    msg_id = f"res_board_{bid}"
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return json.dumps({"error": "requires_auth", "message": "ESPHome требует аутентификацию"}, ensure_ascii=False)

            await ws.send(json.dumps({
                "command": "boards/get_board",
                "message_id": msg_id,
                "args": {"board_id": bid}
            }))

            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") != msg_id:
                    continue
                if data.get("error_code"):
                    return json.dumps({"error": data.get("error_code"), "details": data.get("details", data)}, ensure_ascii=False)
                result = data.get("result", {})
                return json.dumps(result, indent=2, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Ошибка получения ресурса esphome://boards/{bid}: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ==============================================================================
# РАЗДЕЛ: ИНТЕРАКТИВНЫЕ ПРОМПТЫ MCP (MCP Prompts, Фаза 4.2)
# ==============================================================================

@mcp.prompt(name="new_device_wizard", description="Интерактивный мастер проектирования и создания нового устройства ESPHome")
def prompt_new_device_wizard(
    board_id: str = "",
    device_name: str = "",
    friendly_name: str = "",
    components: str = ""
) -> str:
    """
    Промпт: Мастер проектирования и создания нового устройства ESPHome.
    """
    b_info = f"`{board_id}`" if board_id else "не указана (уточни у пользователя или подбери через get_board_info)"
    d_name = f"`{device_name}`" if device_name else "не указано (сгенерируй подходящее имя в kebab-case)"
    f_name = f"`{friendly_name}`" if friendly_name else "не указано"
    c_list = f"`{components}`" if components else "не указаны"

    return f"""Ты — ведущий инженер по встраиваемым системам и конфигурации устройств ESPHome.
Пользователь обратился с задачей спроектировать и подготовить новое устройство умного дома.

📋 Параметры целевого устройства:
- Идентификатор платы (board_id): {b_info}
- Имя конфигурации (device_name): {d_name}
- Дружественное имя (friendly_name): {f_name}
- Требуемые компоненты / датчики: {c_list}

Выполни пошаговое проектирование строго по следующему регламенту:
1. **Аудит платы и аппаратных пинов:**
   - Если плата задана, изучи её распиновку через ресурс `esphome://boards/{board_id or 'esp32dev'}` или инструмент `get_board_info(action="get")`.
   - Выяви доступные GPIO, исключая служебные пины SPI Flash (GPIO6-11 на ESP32) и strapping-пины.
2. **Проверка спецификаций компонентов:**
   - Для каждого датчика проверь паспорт через `esphome://components/<component_id>` или `search_components(action="get")`.
   - Определи требования к шинам (I2C SDA/SCL, SPI MOSI/MISO/CLK, UART TX/RX) и выдели неконфликтующие пины.
3. **Генерация конфигурации:**
   - Создай файл конфигурации через `manage_device_config(action="create")` с шаблоном платы и безопасным Wi-Fi через secrets.
   - Либо добавь компоненты по очереди через `manage_device_config(action="add_component")`.
4. **Валидация синтаксиса:**
   - Обязательно запусти `validate_yaml` для проверки целостности созданного конфига.
5. **Тестовая сборка и аудит памяти:**
   - Скомпилируй прошивку через `compile_firmware` и оцени расход памяти через `manage_build_jobs(action="analyze_memory")`.

Предоставь пользователю итоговый отчет с описанием подключения датчиков к пинам платы и готовностью к прошивке."""


@mcp.prompt(name="troubleshoot_offline_device", description="Пошаговый чеклист автономной диагностики причин недоступности (offline) устройства")
def prompt_troubleshoot_offline_device(configuration: str) -> str:
    """
    Промпт: Автономная диагностика оффлайн-устройства ESPHome.
    """
    cfg = resolve_configuration(configuration)
    return f"""Ты — дежурный сетевой инженер и специалист по диагностике оборудования ESPHome.
Устройство `{cfg}` в настоящий момент не выходит на связь (статус Offline) или испытывает сетевые сбои.

Выполни глубокое автономное расследование по следующему чеклисту:
1. **Сетевой зонд (L3 / L4 / mDNS):**
   - Вызови инструмент `troubleshoot_device(action="probe", configuration="{cfg}")`.
   - Проанализируй:
     * Доступен ли узел по ICMP Ping и каково время отклика (RTT)?
     * Резолвится ли имя хоста через локальный DNS и через mDNS Zeroconf?
     * Нет ли расхождения между кэшированным IP и текущим адресом?
2. **Анализ истории версий и недавних правок (Git History):**
   - Проверь историю коммитов через `manage_version_history(action="list_versions", configuration="{cfg}", max_count=5)`.
   - Проверь последний diff через `manage_version_history(action="get_diff", configuration="{cfg}")`: какие правки вносились в Wi-Fi, статические IP, шины или интервалы опроса перед сбоем?
3. **Анализ логов и дампов паники:**
   - Если устройство перезагружается в Bootloop, запроси логи запуска через `stream_device_logs(configuration="{cfg}", duration_seconds=5, no_states=True)`.
   - При обнаружении паники ядра (Guru Meditation / Fatal exception) расшифруй дамп через `decode_crash_backtrace(configuration="{cfg}", lines=...)`.
4. **Формулирование диагноза и мер восстановления:**
   - Предоставь пользователю точный диагноз причины сбоя.
   - Если проблема программная, предложи немедленный безопасный откат на стабильную ревизию через `manage_version_history(action="restore", configuration="{cfg}", sha=...)`."""


@mcp.prompt(name="safe_firmware_upgrade", description="Регламент безопасного обновления прошивки ESPHome без риска сбоя")
def prompt_safe_firmware_upgrade(configuration: str, target_port: str = "OTA") -> str:
    """
    Промпт: Регламент безопасного обновления прошивки.
    """
    cfg = resolve_configuration(configuration)
    return f"""Ты — старший инженер по релиз-менеджменту и обновлению парка устройств ESPHome.
Требуется провести процедуру безопасного обновления прошивки для устройства `{cfg}` (целевой адрес/порт: `{target_port}`).

Строго следуй регламенту с нулевым риском потери контроля над устройством:
1. **Предварительный аудит узла (Health Check):**
   - Проверь доступность устройства через `troubleshoot_device(action="probe", configuration="{cfg}")` или `list_devices`.
   - Убедись, что узел находится онлайн и уровень сигнала Wi-Fi достаточен для устойчивой OTA-передачи.
2. **Фиксация точки отката (Safety Snapshot):**
   - Убедись, что текущее рабочее состояние зафиксировано в истории: `manage_version_history(action="list_versions", configuration="{cfg}")`.
3. **Валидация YAML-конфигурации:**
   - Запусти валидацию через `validate_yaml(configuration="{cfg}")`.
   - При наличии устаревших директив проведи автомиграцию синтаксиса через `migrate_device_config(configuration="{cfg}")`.
4. **Сборка и аудит разделов памяти:**
   - Запусти компиляцию без загрузки: `compile_firmware(configuration="{cfg}")`.
   - Оцени профиль памяти через `manage_build_jobs(action="analyze_memory", configuration="{cfg}")`: убедись, что размер бинарника не превышает лимиты OTA-партиции.
5. **OTA-прошивка:**
   - Запусти прошивку через `flash_ota(configuration="{cfg}", port="{target_port}")`.
   - Дождись успешного завершения передачи.
6. **Верификация старта (Post-Flash Verification):**
   - Подключись к потоку логов устройства через `stream_device_logs(configuration="{cfg}", duration_seconds=10, no_states=True)`.
   - Убедись в штатном запуске компонентов, успешном подключении к сети и отсутствии циклов перезагрузок."""


# ==============================================================================
# ФАЗА 5: АППАРАТНЫЙ АУДИТ И БЕЗОПАСНОСТЬ РАСПИНОВКИ (Phase 5.1.1)
# ==============================================================================

def normalize_gpio(pin_val: str | int) -> int | None:
    """Нормализует строковое или числовое представление GPIO к целому числу."""
    if isinstance(pin_val, int):
        return pin_val
    if not pin_val:
        return None
    s = str(pin_val).strip().upper().strip('"\'')
    if s.startswith("GPIO"):
        s = s[4:].strip()
    d_map = {
        "D0": 16, "D1": 5, "D2": 4, "D3": 0, "D4": 2,
        "D5": 14, "D6": 12, "D7": 13, "D8": 15, "D9": 3, "D10": 1
    }
    if s in d_map:
        return d_map[s]
    try:
        return int(s)
    except ValueError:
        return None


def analyze_yaml_pinout_and_rules(
    yaml_text: str,
    board_id_override: str = "",
    board_info: dict | None = None
) -> dict:
    """
    Выполняет статический анализ YAML-конфигурации: извлекает используемые пины,
    роли компонентов, I2C адреса и сопоставляет с аппаратной матрицей ограничений.
    """
    substitutions: dict[str, str] = {}
    lines = yaml_text.splitlines()

    # 1. Извлекаем substitutions
    in_subst = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "substitutions:":
            in_subst = True
            continue
        if in_subst:
            if not line.startswith(" ") and not line.startswith("\t"):
                in_subst = False
            else:
                m = re.match(r"^\s*([a-zA-Z0-9_-]+):\s*[\"']?([^\"'#\n]+)[\"']?", line)
                if m:
                    substitutions[m.group(1).strip()] = m.group(2).strip()

    def resolve_subst(val: str) -> str:
        if not isinstance(val, str):
            return val
        for k, v in substitutions.items():
            val = val.replace(f"${{{k}}}", v).replace(f"${k}", v)
        return val

    # 2. Определяем платформу и целевую плату
    platform = "esp32"
    board_id = board_id_override

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("esp32:"):
            platform = "esp32"
        elif stripped.startswith("esp8266:"):
            platform = "esp8266"
        elif stripped.startswith("rp2040:"):
            platform = "rp2040"

        m_board = re.search(r"^\s*board:\s*[\"']?([^\"'#\n]+)[\"']?", line)
        if m_board and not board_id:
            board_id = resolve_subst(m_board.group(1).strip())

    # 3. Извлекаем пины и адреса I2C
    used_pins: list[dict] = []
    i2c_devices: list[dict] = []

    current_section = ""
    current_platform = ""
    current_comp_id = ""

    pin_regex = re.compile(
        r"^\s*(sda|scl|clk_pin|mosi_pin|miso_pin|cs_pin|tx_pin|rx_pin|data_pin|clock_pin|echo_pin|trigger_pin|reset_pin|rst_pin|pin|number):\s*[\"']?([^\"'#\n]+)[\"']?",
        re.IGNORECASE
    )
    addr_regex = re.compile(r"^\s*address:\s*[\"']?(0x[0-9a-fA-F]+|[0-9]+)[\"']?", re.IGNORECASE)

    for idx, raw_line in enumerate(lines, 1):
        line = raw_line.split("#")[0].rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        # Секция верхнего уровня
        if not raw_line.startswith(" ") and not raw_line.startswith("\t") and ":" in stripped:
            sec = stripped.split(":")[0].strip()
            current_section = sec
            current_platform = ""
            current_comp_id = ""
            continue

        # Вложенная platform
        m_plat = re.match(r"^\s*-\s*platform:\s*([a-zA-Z0-9_-]+)", line) or re.match(r"^\s*platform:\s*([a-zA-Z0-9_-]+)", line)
        if m_plat:
            current_platform = m_plat.group(1).strip()

        m_id = re.match(r"^\s*id:\s*([a-zA-Z0-9_-]+)", line)
        if m_id:
            current_comp_id = m_id.group(1).strip()

        # Поиск I2C адреса
        m_addr = addr_regex.search(line)
        if m_addr:
            raw_addr = m_addr.group(1).strip()
            try:
                addr_int = int(raw_addr, 16) if raw_addr.lower().startswith("0x") else int(raw_addr)
                addr_hex = f"0x{addr_int:02X}"
                comp_name = current_comp_id or current_platform or current_section or f"line_{idx}"
                i2c_devices.append({
                    "address": addr_hex,
                    "address_int": addr_int,
                    "component": comp_name,
                    "line_number": idx
                })
            except ValueError:
                pass

        # Поиск директив пинов
        m_pin = pin_regex.search(line)
        if m_pin:
            field_name = m_pin.group(1).lower()
            raw_val = resolve_subst(m_pin.group(2).strip())
            gpio_num = normalize_gpio(raw_val)
            if gpio_num is not None:
                if field_name == "sda":
                    role = "I2C SDA"
                elif field_name == "scl":
                    role = "I2C SCL"
                elif field_name in ("tx_pin", "tx"):
                    role = "UART TX"
                elif field_name in ("rx_pin", "rx"):
                    role = "UART RX"
                elif field_name in ("clk_pin", "mosi_pin", "cs_pin"):
                    role = "SPI OUT"
                elif field_name == "miso_pin":
                    role = "SPI IN"
                elif current_section in ("switch", "output", "light"):
                    role = "OUTPUT"
                elif current_platform == "adc":
                    role = "ADC"
                elif current_section in ("binary_sensor", "sensor"):
                    role = "INPUT"
                else:
                    role = "GPIO"

                comp_name = current_comp_id or current_platform or current_section or f"line_{idx}"
                used_pins.append({
                    "gpio": gpio_num,
                    "role": role,
                    "component": comp_name,
                    "field": field_name,
                    "line_number": idx
                })

    # 4. База аппаратных ограничений
    FLASH_PINS = {
        "esp32": {6, 7, 8, 9, 10, 11},
        "esp8266": {6, 7, 8, 9, 10, 11},
        "esp32c3": {12, 13, 14, 15, 16, 17},
        "esp32s3": {26, 27, 28, 29, 30, 31, 32}
    }
    INPUT_ONLY_PINS = {
        "esp32": {34, 35, 36, 39}
    }
    STRAPPING_PINS = {
        "esp32": {
            0: "Boot mode select (должен быть HIGH/Floating для нормального старта)",
            2: "Boot mode select (должен быть LOW/Floating для старта из Flash)",
            12: "MTDI — критический выбор напряжения Flash (3.3V vs 1.8V). Высокий уровень при старте может сжечь чип/память!",
            15: "MTDO — управление тишиной логов загрузчика"
        },
        "esp8266": {
            0: "Boot mode (HIGH для старта, LOW для прошивки)",
            2: "Boot mode (HIGH для старта)",
            15: "Boot mode (должен быть LOW при старте)"
        }
    }
    ADC2_PINS = {
        "esp32": {0, 2, 4, 12, 13, 14, 15, 25, 26, 27}
    }

    board_flash_pins = set()
    board_input_only = set()
    board_strapping = {}
    board_pin_notes = {}

    if board_info and isinstance(board_info, dict):
        for p in board_info.get("pins", []):
            g = p.get("gpio")
            if g is None:
                continue
            if p.get("available") is False and "flash" in p.get("occupied_by", "").lower():
                board_flash_pins.add(g)
            feats = [f.lower() for f in p.get("features", [])]
            if "input_only" in feats:
                board_input_only.add(g)
            if "strapping" in feats:
                board_strapping[g] = p.get("notes", "Strapping pin")
            if p.get("notes"):
                board_pin_notes[g] = p.get("notes")

    effective_flash = board_flash_pins or FLASH_PINS.get(platform, set())
    effective_input_only = board_input_only or INPUT_ONLY_PINS.get(platform, set())
    effective_strapping = board_strapping or STRAPPING_PINS.get(platform, {})

    fatal_errors = []
    warnings = []

    # Проверка 1: Flash Pins (FATAL)
    for p in used_pins:
        g = p["gpio"]
        if g in effective_flash:
            fatal_errors.append(
                f"❌ **Критический конфликт SPI Flash (GPIO{g}):** Пин назначен компоненту `{p['component']}` (строка {p['line_number']}). "
                f"На платформе {platform.upper()} пины {sorted(list(effective_flash))} зарезервированы под встроенную SPI Flash память. "
                f"Использование GPIO{g} приведет к аппаратному сбою шины SPI и бесконечному циклу перезагрузок (bootloop)!"
            )

    # Проверка 2: Input-Only Pins в роли выходов (FATAL)
    for p in used_pins:
        g = p["gpio"]
        role = p["role"]
        if g in effective_input_only and role in ("OUTPUT", "SPI OUT", "I2C SDA", "I2C SCL", "UART TX"):
            fatal_errors.append(
                f"❌ **Аппаратная невозможность вывода (GPIO{g}):** Пин назначен как `{role}` для компонента `{p['component']}` (строка {p['line_number']}). "
                f"На платформе {platform.upper()} GPIO{g} является физически Input-only (нет выходных драйверов и подтяжек) и не может работать как выход или шина I2C!"
            )

    # Проверка 3: Дублирование пинов между компонентами (FATAL)
    pin_usage: dict[int, list[dict]] = {}
    for p in used_pins:
        g = p["gpio"]
        if g not in pin_usage:
            pin_usage[g] = []
        pin_usage[g].append(p)

    for g, usages in pin_usage.items():
        if len(usages) > 1:
            comps = [f"`{u['component']}` ({u['role']}, строка {u['line_number']})" for u in usages]
            fatal_errors.append(
                f"❌ **Конфликт повторного использования (GPIO{g}):** Пин одновременно назначен нескольким компонентам:\n  - " + "\n  - ".join(comps)
            )

    # Проверка 4: Коллизии адресов I2C (FATAL)
    addr_usage: dict[str, list[dict]] = {}
    for dev in i2c_devices:
        a = dev["address"]
        if a not in addr_usage:
            addr_usage[a] = []
        addr_usage[a].append(dev)

    for a, devs in addr_usage.items():
        if len(devs) > 1:
            dev_list = [f"`{d['component']}` (строка {d['line_number']})" for d in devs]
            fatal_errors.append(
                f"❌ **Коллизия адресов шины I2C ({a}):** Несколько устройств на одной шине имеют одинаковый I2C адрес:\n  - " + "\n  - ".join(dev_list) +
                f"\n  *Рекомендация:* Измените адрес через аппаратный пин ADDR датчика либо используйте мультиплексор TCA9548A."
            )

    # Проверка 5: Strapping Pins (WARNING)
    for p in used_pins:
        g = p["gpio"]
        if g in effective_strapping:
            note = effective_strapping[g]
            warnings.append(
                f"⚠️ **Предостережение: Strapping Pin (GPIO{g}):** Назначен компоненту `{p['component']}` (строка {p['line_number']}). "
                f"GPIO{g} участвует в выборе режима загрузки контроллера ({note}). Убедитесь, что внешняя схема не удерживает неверный уровень напряжения при старте питания."
            )

    # Проверка 6: ADC2 & Wi-Fi conflict (WARNING)
    for p in used_pins:
        g = p["gpio"]
        if p["role"] == "ADC" and g in ADC2_PINS.get(platform, set()):
            warnings.append(
                f"⚠️ **Конфликт АЦП ADC2 с Wi-Fi (GPIO{g}):** Пин используется для аналогового считывания `{p['component']}`. "
                f"На ESP32 блок ADC2 аппаратным образом блокируется модулем Wi-Fi. При активном Wi-Fi показания будут сбоить. Рекомендуется перенести датчик на блок ADC1 (GPIO32–GPIO39)."
            )

    status = "FAILED" if fatal_errors else ("WARNINGS" if warnings else "PASSED")

    return {
        "status": status,
        "platform": platform,
        "board_id": board_id,
        "used_pins": used_pins,
        "i2c_devices": i2c_devices,
        "board_pin_notes": board_pin_notes,
        "fatal_errors": fatal_errors,
        "warnings": warnings
    }


@mcp.tool()
async def validate_hardware_pinout(
    configuration: str = "",
    content: str = "",
    board_id: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 28 (Phase 5.1.1): Интеллектуальный аппаратный аудит распиновки GPIO и коллизий шин.

    Проверяет YAML-конфигурацию на критические аппаратные конфликты:
      - Зарезервированные пины SPI Flash памяти (GPIO6-GPIO11 на ESP32)
      - Назначение физически входных (Input-only GPIO34-GPIO39) пинов в качестве выходов/ШИМ/I2C
      - Конфликты Strapping-пинов режима начальной загрузки (GPIO0, GPIO2, GPIO12, GPIO15)
      - Конфликты аналоговых каналов блока ADC2 при одновременной работе с Wi-Fi
      - Дублирование аппаратных пинов между несколькими компонентами
      - Аппаратные коллизии одинаковых адресов нескольких датчиков на одной шине I2C

    Параметры:
      - configuration: имя файла конфигурации устройства (например, "test.yaml")
      - content:       опциональный сырой текст YAML для проверки до сохранения
      - board_id:      опциональный ID платы (например, "esp32dev", "d1_mini", "nodemcu-32s")
      - host:          хост сервера ESPHome
      - port:          сетевой порт WebSocket API ESPHome
    """
    if not configuration and not content:
        return "Ошибка: необходимо указать либо параметр configuration (имя файла), либо content (текст YAML)."

    url = get_ws_url(host, port)
    yaml_text = content
    target_config = resolve_configuration(configuration) if configuration else ""

    # Если content не задан, считываем YAML через devices/get_config
    if not yaml_text and target_config:
        try:
            async with websockets.connect(url, ping_interval=None) as ws:
                first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                first_msg = json.loads(first_msg_raw)
                if first_msg.get("requires_auth"):
                    return f"Ошибка: ESPHome на {host}:{port} требует аутентификации (requires_auth=true)."

                msg_id = "pinout_get_cfg_1"
                await ws.send(json.dumps({
                    "command": "devices/get_config",
                    "message_id": msg_id,
                    "args": {"configuration": target_config}
                }))

                async for message in ws:
                    data = json.loads(message)
                    if data.get("message_id") != msg_id:
                        continue
                    if data.get("error_code"):
                        return f"Ошибка получения конфигурации {target_config}: {data.get('details', data)}"
                    res = data.get("result", "")
                    yaml_text = res if isinstance(res, str) else json.dumps(res)
                    break

        except Exception as e:
            return f"Критическая ошибка получения конфигурации {target_config} ({url}): {str(e)}"

    if not yaml_text:
        return f"Ошибка: не удалось прочитать содержимое конфигурации `{target_config}`."

    # Пытаемся получить паспорт платы через boards/get_board для расширения распиновки
    board_info = None
    target_board = board_id

    # Если board_id не передан явно, извлекаем его предварительно из YAML
    if not target_board:
        m = re.search(r"^\s*board:\s*[\"']?([^\"'#\n]+)[\"']?", yaml_text, re.MULTILINE)
        if m:
            target_board = m.group(1).strip()
            m_sub = re.search(r"substitutions:\s*\n((?:\s+[a-zA-Z0-9_-]+:\s*[^\n]+\n)+)", yaml_text)
            if m_sub and "${" in target_board:
                for line in m_sub.group(1).splitlines():
                    sm = re.match(r"^\s*([a-zA-Z0-9_-]+):\s*[\"']?([^\"'#\n]+)[\"']?", line)
                    if sm:
                        target_board = target_board.replace(f"${{{sm.group(1).strip()}}}", sm.group(2).strip())

    if target_board and not target_board.startswith("$"):
        try:
            async with websockets.connect(url, ping_interval=None) as ws:
                await asyncio.wait_for(ws.recv(), timeout=2.0)
                b_id = "pinout_get_board_1"
                await ws.send(json.dumps({
                    "command": "boards/get_board",
                    "message_id": b_id,
                    "args": {"board_id": target_board}
                }))
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                if not data.get("error_code"):
                    board_info = data.get("result", {})
        except Exception:
            pass

    # Выполняем анализ
    res = analyze_yaml_pinout_and_rules(yaml_text, board_id_override=target_board, board_info=board_info)

    status = res["status"]
    platform = res["platform"]
    board_name = res["board_id"] or target_board or "не определена"
    used_pins = res["used_pins"]
    i2c_devices = res["i2c_devices"]
    fatal_errors = res["fatal_errors"]
    warnings = res["warnings"]
    pin_notes = res.get("board_pin_notes", {})

    status_badge = {
        "PASSED": "🟢 **PASSED (Аппаратных коллизий не обнаружено)**",
        "WARNINGS": "🟡 **WARNINGS (Обнаружены предупреждения и аппаратные нюансы)**",
        "FAILED": "🔴 **FAILED (ОБНАРУЖЕНЫ КРИТИЧЕСКИЕ АППАРАТНЫЕ ОШИБКИ)**"
    }.get(status, status)

    header = f"### ⚡ Аппаратный аудит распиновки GPIO (validate_hardware_pinout)\n\n"
    header += f"- **Целевая конфигурация:** `{target_config or 'переданный текст YAML'}`\n"
    header += f"- **Платформа:** `{platform.upper()}` | **Плата:** `{board_name}`\n"
    header += f"- **Итоговый вердикт:** {status_badge}\n\n"

    output = [header]

    # Сводная таблица используемых пинов
    output.append(f"#### 📋 Задействованные пины GPIO ({len(used_pins)} шт.):\n")
    if not used_pins:
        output.append("*В конфигурации не обнаружено явных определений аппаратных пинов.*")
    else:
        output.append("| GPIO | Роль в схеме | Компонент | Строка | Аппаратные примечания |")
        output.append("|:---:|:---:|---|:---:|---|")
        for p in sorted(used_pins, key=lambda x: x["gpio"]):
            g = p["gpio"]
            note = pin_notes.get(g, "")
            output.append(f"| **GPIO{g}** | `{p['role']}` | `{p['component']}` | {p['line_number']} | {note} |")

    # Сводка по I2C
    if i2c_devices:
        output.append(f"\n#### 🔍 Устройства на шине I2C ({len(i2c_devices)} шт.):\n")
        output.append("| I2C Адрес | Компонент | Строка | Статус |")
        output.append("|:---:|---|:---:|---|")
        seen_addrs = set()
        dup_addrs = set()
        for d in i2c_devices:
            if d["address"] in seen_addrs:
                dup_addrs.add(d["address"])
            seen_addrs.add(d["address"])

        for d in i2c_devices:
            st = "❌ **КОЛЛИЗИЯ**" if d["address"] in dup_addrs else "🟢 OK"
            output.append(f"| `{d['address']}` | `{d['component']}` | {d['line_number']} | {st} |")

    # Секция критических ошибок
    if fatal_errors:
        output.append("\n---\n### ❌ Критические аппаратные конфликты (FATAL):")
        output.append("> ⛔ **Внимание:** Компиляция и прошивка конфигурации с данными ошибками приведет к сбою загрузки микроконтроллера!\n")
        for err in fatal_errors:
            output.append(f"- {err}\n")

    # Секция предупреждений
    if warnings:
        output.append("\n---\n### ⚠️ Предостережения и рекомендации (WARNINGS):\n")
        for w in warnings:
            output.append(f"- {w}\n")

    return "\n".join(output)


# =========================================================================
# ФАЗА 5.3: БЕЗОПАСНАЯ CANARY OTA-ПРОШИВКА С АВТО-ОТКАТОМ ПРИ BOOTLOOP
# =========================================================================

def analyze_canary_health(logs: list[str]) -> dict:
    """
    Анализирует строки лога первого запуска на маркеры фатальных аварий и бутлупа:
      - Guru Meditation Panic / Core Dump
      - Backtrace Stack
      - abort() / assert failed
      - WDT Reset (Panic / Task Watchdog)
      - Вход в Safe Mode ESPHome
      - Повреждение динамической памяти (Heap Corruption)
      - Множественные рестарты (Bootloop)
    """
    crash_markers: list[str] = []
    backtrace_lines: list[str] = []
    reset_count = 0
    in_safe_mode = False

    guru_re = re.compile(r"Guru Meditation Error|Core\s+\d+\s+panic", re.IGNORECASE)
    bt_re = re.compile(r"Backtrace:\s*(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+\s*)+", re.IGNORECASE)
    abort_re = re.compile(r"abort\(\) was called|assert(?:ion)?\s+.*failed", re.IGNORECASE)
    wdt_re = re.compile(r"rst:0x[0-9a-fA-F]+\s*\((?:TG[01]WDT_SYS_RESET|RTCWDT_RTC_RESET|PANIC)\)", re.IGNORECASE)
    safe_re = re.compile(r"ESPHome is in safe mode|Unresolved crash loop detected", re.IGNORECASE)
    heap_re = re.compile(r"CORRUPT HEAP|heap_caps_alloc failed", re.IGNORECASE)
    rst_any_re = re.compile(r"rst:0x[0-9a-fA-F]+|Booting ESPHome", re.IGNORECASE)

    for line in logs:
        clean_line = line.strip()
        if not clean_line:
            continue
        if guru_re.search(clean_line):
            crash_markers.append(f"Guru Meditation Panic: `{clean_line}`")
        if bt_re.search(clean_line):
            backtrace_lines.append(clean_line)
            crash_markers.append(f"Backtrace Stack: `{clean_line}`")
        if abort_re.search(clean_line):
            crash_markers.append(f"Assertion/Abort: `{clean_line}`")
        if wdt_re.search(clean_line):
            crash_markers.append(f"Watchdog Reset: `{clean_line}`")
        if safe_re.search(clean_line):
            in_safe_mode = True
            crash_markers.append(f"Safe Mode Triggered: `{clean_line}`")
        if heap_re.search(clean_line):
            crash_markers.append(f"Heap Corruption: `{clean_line}`")
        if rst_any_re.search(clean_line):
            reset_count += 1

    if reset_count > 2:
        crash_markers.append(f"Bootloop Detection: обнаружено {reset_count} циклов рестарта за период наблюдения")

    is_healthy = len(crash_markers) == 0 and not in_safe_mode

    return {
        "is_healthy": is_healthy,
        "crash_markers": crash_markers,
        "backtrace_lines": backtrace_lines,
        "reset_count": reset_count,
        "in_safe_mode": in_safe_mode,
        "total_lines": len(logs)
    }


async def _get_raw_device_config(host: str, port: int, configuration: str) -> tuple[bool, str]:
    """Вспомогательная функция считывания сырого YAML устройства через devices/get_config."""
    url = get_ws_url(host, port)
    msg_id = f"canary_get_cfg_{uuid.uuid4().hex[:8]}"
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            if first_msg.get("requires_auth"):
                return False, "requires_auth"
            await ws.send(json.dumps({
                "command": "devices/get_config",
                "message_id": msg_id,
                "args": {"configuration": configuration}
            }))
            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") == msg_id:
                    if data.get("error_code"):
                        return False, str(data.get("details", data))
                    return True, str(data.get("result", ""))
    except Exception as e:
        return False, str(e)
    return False, "timeout"


async def _update_raw_device_config(host: str, port: int, configuration: str, content: str) -> tuple[bool, str]:
    """Вспомогательная функция записи сырого YAML через devices/update_config."""
    url = get_ws_url(host, port)
    msg_id = f"canary_upd_cfg_{uuid.uuid4().hex[:8]}"
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            if first_msg.get("requires_auth"):
                return False, "requires_auth"
            await ws.send(json.dumps({
                "command": "devices/update_config",
                "message_id": msg_id,
                "args": {"configuration": configuration, "content": content, "allow_wipe": True}
            }))
            async for message in ws:
                data = json.loads(message)
                if data.get("message_id") == msg_id:
                    if data.get("error_code"):
                        return False, str(data.get("details", data))
                    return True, "OK"
    except Exception as e:
        return False, str(e)
    return False, "timeout"


async def _collect_device_logs(host: str, port: int, configuration: str, target_port: str, duration_seconds: int = 15) -> list[str]:
    """Вспомогательная функция сторожевого сбора логов устройства за заданный интервал времени."""
    url = get_ws_url(host, port)
    msg_id = f"canary_watch_logs_{uuid.uuid4().hex[:8]}"
    output_lines: list[str] = []
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            if first_msg.get("requires_auth"):
                return ["ERROR: requires_auth"]
            await ws.send(json.dumps({
                "command": "devices/logs",
                "message_id": msg_id,
                "args": {"configuration": configuration, "port": target_port, "no_states": True}
            }))
            start_time = asyncio.get_event_loop().time()
            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                remaining = duration_seconds - elapsed
                if remaining <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
                except asyncio.TimeoutError:
                    break
                data = json.loads(msg)
                if data.get("message_id") == msg_id and data.get("event") == "output":
                    line_data = data.get("data", "")
                    if isinstance(line_data, dict):
                        line = line_data.get("line", "").strip()
                    else:
                        line = str(line_data).strip()
                    if line:
                        output_lines.append(clean_ansi(line))
    except Exception as e:
        output_lines.append(f"LOG_ERROR: {str(e)}")
    return output_lines


@mcp.tool()
async def flash_canary_ota(
    configuration: str,
    port: str = "OTA",
    compile_first: bool = True,
    monitor_seconds: int = 15,
    auto_rollback: bool = True,
    force_local: bool = False,
    bootloader: bool = False,
    host: str = DEFAULT_HOST,
    api_port: int = DEFAULT_PORT
) -> str:
    """
    Инструмент 29 (Phase 5.3.1): Безопасная Canary OTA-прошивка со сторожевым мониторингом запуска и авто-откатом при Bootloop.

    Реализует защищенный конвейер обновления микроконтроллеров ESPHome:
      1. Снятие контрольного снимка (Baseline Snapshot): сохранение исходного рабочего YAML через devices/get_config.
      2. Компиляция и загрузка (Deploy): сборка прошивки (при compile_first=True) и OTA-загрузка на узел.
         При ошибках компиляции или обрыве связи процесс безопасно останавливается без риска для контроллера.
      3. Сторожевой мониторинг (Canary Watchdog): прослушивание runtime-логов первого запуска в течение monitor_seconds.
      4. Детекция аварий ядра: выявление Guru Meditation Panic, Backtrace, Core Dump, WDT Bootloop и Safe Mode.
      5. Автоматический откат (Safe Rollback): при обнаружении фатального сбоя и auto_rollback=True конфигурация
         немедленно восстанавливается из контрольного снимка, повторно компилируется и прошивается обратно
         (используя технологическое окно Safe Mode ESPHome OTA).

    Параметры:
      - configuration:   имя файла конфигурации ("test.yaml" или с префиксом "config/test.yaml")
      - port:            целевой порт или сетевой IP/hostname устройства (по умолчанию "OTA")
      - compile_first:   выполнять ли сборку перед прошивкой (по умолчанию True)
      - monitor_seconds: время сторожевого мониторинга логов запуска в секундах (по умолчанию 15)
      - auto_rollback:   выполнять ли автоматический откат при обнаружении паники ядра (по умолчанию True)
      - force_local:     принудительная локальная сборка без кэша/кластера (по умолчанию False)
      - bootloader:      прошивка bootloader (по умолчанию False)
      - host:            хост сервера ESPHome (по умолчанию из .env или localhost)
      - api_port:        сетевой порт WebSocket API ESPHome (по умолчанию из .env или 6052)
    """
    config_name = resolve_configuration(configuration)
    logger.info(f"Запуск Canary OTA прошивки {config_name} (порт: {port}, мониторинг: {monitor_seconds}s, откат: {auto_rollback})...")

    # Шаг 1. Фиксация Baseline (контрольного снимка)
    baseline_ok, baseline_yaml = await _get_raw_device_config(host, api_port, config_name)
    if not baseline_ok:
        logger.warning(f"Не удалось получить baseline snapshot для {config_name}: {baseline_yaml}")

    # Шаг 2. Компиляция (если запрошена)
    if compile_first:
        logger.info(f"Canary Deploy: компиляция прошивки {config_name}...")
        compile_res = await compile_firmware(configuration=config_name, force_local=force_local, host=host, port=api_port)
        if "ОШИБКА" in compile_res or "Критическая ошибка" in compile_res:
            return (
                f"### 🐥 Результаты безопасной Canary OTA-прошивки (flash_canary_ota)\n\n"
                f"- **Целевая конфигурация:** `{config_name}`\n"
                f"- **Итоговый вердикт:** 🔴 **СБОРКА НЕ УДАЛАСЬ (DECOMPILATION FAILED)**\n\n"
                f"> ⛔ **Безопасная остановка:** Компиляция новой версии завершилась ошибкой. Загрузка прошивки отменена, "
                f"устройство не подвергалось изменениям и продолжает работать в штатном режиме.\n\n"
                f"**Лог компиляции:**\n{compile_res}"
            )

    # Шаг 3. Загрузка прошивки по OTA
    logger.info(f"Canary Deploy: OTA-загрузка прошивки {config_name} на порт {port}...")
    flash_res = await flash_ota(configuration=config_name, port=port, bootloader=bootloader, host=host, api_port=api_port)
    if "ОШИБКА" in flash_res or "Критическая ошибка" in flash_res:
        return (
            f"### 🐥 Результаты безопасной Canary OTA-прошивки (flash_canary_ota)\n\n"
            f"- **Целевая конфигурация:** `{config_name}` (порт: `{port}`)\n"
            f"- **Итоговый вердикт:** 🔴 **ОШИБКА ЗАГРУЗКИ (UPLOAD FAILED)**\n\n"
            f"> ℹ️ Прошивка не была передана на устройство (сетевой таймаут или отказ связи). "
            f"Откат не требуется, устройство сохранило предыдущую рабочую версию.\n\n"
            f"**Лог процесса загрузки:**\n{flash_res}"
        )

    # Шаг 4. Ожидание перезагрузки микроконтроллера и подъем Wi-Fi/Ethernet стека
    logger.info(f"Canary Watchdog: пауза 4 секунды для перезагрузки узла {config_name}...")
    await asyncio.sleep(4)

    # Шаг 5. Сторожевой сбор и мониторинг логов первого запуска
    logger.info(f"Canary Watchdog: сбор логов {config_name} на протяжении {monitor_seconds} сек...")
    startup_logs = await _collect_device_logs(host, api_port, config_name, port, duration_seconds=monitor_seconds)

    # Шаг 6. Интеллектуальный анализ здоровья запуска
    health = analyze_canary_health(startup_logs)

    # Ветка 1: Обнаружен критический сбой (Guru Meditation / Bootloop / Safe Mode)
    if not health["is_healthy"]:
        logger.error(f"Canary Watchdog: ОБНАРУЖЕН КРАШ УЗЛА {config_name}! Маркеры: {health['crash_markers']}")
        report = [
            f"### 🐥 Результаты безопасной Canary OTA-прошивки (flash_canary_ota)\n",
            f"- **Целевая конфигурация:** `{config_name}` (порт: `{port}`)",
            f"- **Итоговый вердикт:** 🔴 **CANARY FAILED (ОБНАРУЖЕНА КРИТИЧЕСКАЯ АВАРИЯ / BOOTLOOP)**\n",
            f"#### ❌ Обнаруженные маркеры сбоев ядра:"
        ]
        for marker in health["crash_markers"]:
            report.append(f"- 💥 {marker}")

        # Расшифровка стектрейса при наличии
        if health["backtrace_lines"]:
            try:
                decoded_bt = await decode_crash_backtrace(configuration=config_name, lines=health["backtrace_lines"][:10], host=host, port=api_port)
                report.append(f"\n#### 🔍 Расшифровка стека вызовов паники (Backtrace):\n{decoded_bt}\n")
            except Exception as e:
                report.append(f"\n*Не удалось автоматически расшифровать Backtrace: {e}*\n")

        # Процедура автоматического отката (Auto-Rollback)
        if auto_rollback and baseline_ok and baseline_yaml:
            report.append("---\n### 🛡️ Автоматический откат конфигурации и прошивки (Auto-Rollback):\n")
            logger.info(f"Canary Rollback: восстановление исходной конфигурации {config_name}...")
            rb_upd_ok, rb_upd_msg = await _update_raw_device_config(host, api_port, config_name, baseline_yaml)
            if rb_upd_ok:
                report.append("- **Восстановление YAML:** 🟢 Исходный рабочий конфигурационный файл успешно восстановлен из снимка")
                # Компиляция стабильной версии
                logger.info(f"Canary Rollback: компиляция стабильной версии {config_name}...")
                rb_comp = await compile_firmware(configuration=config_name, force_local=force_local, host=host, port=api_port)
                if "УСПЕШНО" in rb_comp:
                    report.append("- **Сборка стабильной прошивки:** 🟢 УСПЕШНО скомпилирована")
                    # Экстренная OTA-прошивка в Safe Mode
                    logger.info(f"Canary Rollback: экстренная OTA-прошивка узла {config_name}...")
                    rb_flash = await flash_ota(configuration=config_name, port=port, bootloader=bootloader, host=host, api_port=api_port)
                    if "УСПЕШНО" in rb_flash:
                        report.append("- **Экстренная OTA-прошивка:** 🟢 УСПЕШНО загружена на узел")
                        report.append("\n> 🟢 **Устройство спасено:** Контроллер возвращен на стабильную прошивку. Риск потери связи полностью устранен!")
                    else:
                        report.append(f"- **Экстренная OTA-прошивка:** 🔴 Ошибка загрузки: {rb_flash}")
                else:
                    report.append(f"- **Сборка стабильной прошивки:** 🔴 Ошибка компиляции: {rb_comp}")
            else:
                report.append(f"- **Восстановление YAML:** 🔴 Не удалось восстановить файл: {rb_upd_msg}")
        elif not auto_rollback:
            report.append("\n> ⚠️ Авто-откат был отключен (`auto_rollback=False`). Контроллер остался в текущем состоянии (возможно в ESPHome Safe Mode).")
        else:
            report.append("\n> ⚠️ Авто-откат не выполнен, так как baseline snapshot конфигурации отсутствовал.")

        return "\n".join(report)

    # Ветка 2: Успешный запуск узла (Canary Passed)
    log_tail_lines = startup_logs[-15:] if startup_logs else ["(логов не зафиксировано за время мониторинга)"]
    log_tail_str = "\n".join(log_tail_lines)

    return (
        f"### 🐥 Результаты безопасной Canary OTA-прошивки (flash_canary_ota)\n\n"
        f"- **Целевая конфигурация:** `{config_name}` (порт: `{port}`)\n"
        f"- **Статус сборки и загрузки:** 🟢 УСПЕШНО\n"
        f"- **Время сторожевого мониторинга:** {monitor_seconds} сек.\n"
        f"- **Итоговый вердикт Canary Watchdog:** 🟢 **CANARY PASSED (Устройство работает стабильно)**\n\n"
        f"#### 📋 Метрики здоровья запуска:\n"
        f"- Собрано строк телеметрии: {health['total_lines']}\n"
        f"- Аппаратных паник / Guru Meditation: 0\n"
        f"- Аварийных рестартов (WDT/Panic): 0\n"
        f"- Safe Mode: Не активирован\n\n"
        f"#### 📜 Лог первого запуска устройства (tail 15 строк):\n"
        f"```text\n{log_tail_str}\n```"
    )


@mcp.tool()
async def get_server_version() -> str:
    """
    Инструмент 28: Получение информации о версии MCP-сервера, протоколе, ресурсах и промптах.

    Возвращает текущую версию сервера (SemVer), статус компонентов, ресурсы и интерактивные промпты.
    """
    return (
        f"### ESPHome Local MCP Server\n"
        f"- **Версия сервера:** `v{__version__}`\n"
        f"- **Стандарт версионирования:** Semantic Versioning 2.0.0 (SemVer)\n"
        f"- **Хост по умолчанию:** `{DEFAULT_HOST}:{DEFAULT_PORT}`\n"
        f"- **Транспорт:** stdio\n"
        f"- **Инструментов (Tools):** 29\n"
        f"- **Контекстные ресурсы (Resources):** `esphome://devices`, `esphome://configs/{{configuration}}`, `esphome://components/{{component_id}}`, `esphome://boards/{{board_id}}`\n"
        f"- **Интерактивные промпты (Prompts):** `new_device_wizard`, `troubleshoot_offline_device`, `safe_firmware_upgrade`"
    )

if __name__ == "__main__":
    logger.info(f"Запуск ESPHome Local MCP Server v{__version__} (stdio)...")
    mcp.run(transport='stdio')

