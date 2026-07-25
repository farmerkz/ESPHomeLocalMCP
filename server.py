from mcp.server.fastmcp import FastMCP
import asyncio
import websockets
import json
import sys
import logging
import re
import os
import uuid

# 1. ЗАЩИТА ПРОТОКОЛА: Перенаправляем все логи в stderr.
# Обычный print() использовать категорически запрещено!
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("esphome-mcp")

# Инициализация MCP сервера
mcp = FastMCP("esphome-device-builder")

def clean_ansi(text: str) -> str:
    text = re.sub(r'\\(?:033|x1b|e)', '\x1b', text)
    ansi_escape = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
    return ansi_escape.sub('', text)

def resolve_configuration(configuration: str) -> tuple[str, str | None]:
    """
    Нормализует параметр configuration.
    
    Возвращает кортеж (config_name, file_content):
      - config_name: имя файла для передачи в ESPHome API
      - file_content: содержимое файла (только для абсолютных путей),
                      None — если файл уже должен быть в конфиг-директории ESPHome
    
    Правила:
      - "config/foo.yaml"    → ("foo.yaml", None)  — обрезаем префикс config/
      - "foo.yaml"           → ("foo.yaml", None)  — уже корректное имя
      - "/abs/path/foo.yaml" → ("foo.yaml", <содержимое файла>)  — абсолютный путь
    """
    # Убираем префикс config/
    if configuration.startswith("config/"):
        configuration = configuration[7:]

    # Если путь абсолютный — читаем содержимое файла с диска
    if os.path.isabs(configuration):
        config_name = os.path.basename(configuration)
        logger.info(f"Абсолютный путь обнаружен: {configuration!r} → имя файла: {config_name!r}")
        try:
            with open(configuration, "r", encoding="utf-8") as f:
                file_content = f.read()
        except OSError as e:
            raise ValueError(f"Не удалось прочитать файл {configuration!r}: {e}")
        return config_name, file_content

    # Относительный путь — передаём как есть
    return configuration, None


async def create_temp_config(ws, file_content: str) -> str:
    """
    Создаёт временный конфиг в ESPHome через devices/create с file_content.
    Возвращает реальное имя созданного временного файла (из ответа API).
    """
    # Используем дефисы: ESPHome slugify убирает подчёркивания в начале имени
    tmp_name = f"mcp-tmp-{uuid.uuid4().hex[:8]}"
    msg_id = "tmp_create"

    logger.info(f"Создаём временный конфиг ESPHome с именем: {tmp_name!r}")
    await ws.send(json.dumps({
        "command": "devices/create",
        "message_id": msg_id,
        "args": {
            "name": tmp_name,
            "file_content": file_content,
            "overwrite": True,
        }
    }))

    # Ждём подтверждения создания
    async for message in ws:
        data = json.loads(message)
        if data.get("message_id") != msg_id:
            continue
        if data.get("error_code"):
            raise RuntimeError(f"Ошибка создания временного конфига: {data.get('details', data)}")
        # ESPHome может slugify имя — берём реальное имя из ответа
        real_name = data.get("result", {}).get("configuration", f"{tmp_name}.yaml")
        logger.info(f"Временный конфиг создан успешно, реальное имя: {real_name!r}")
        return real_name


async def delete_config(ws, config_name: str) -> None:
    """
    Удаляет конфиг в ESPHome через devices/delete.
    """
    msg_id = "tmp_delete"
    logger.info(f"Удаляем временный конфиг ESPHome: {config_name!r}")
    await ws.send(json.dumps({
        "command": "devices/delete",
        "message_id": msg_id,
        "args": {"configuration": config_name}
    }))

    # Ждём подтверждения удаления (не блокируем долго)
    try:
        async for message in ws:
            data = json.loads(message)
            if data.get("message_id") != msg_id:
                continue
            if data.get("error_code"):
                logger.warning(f"Предупреждение при удалении {config_name!r}: {data.get('details', data)}")
            else:
                logger.info(f"Временный конфиг {config_name!r} удалён")
            return
    except Exception as e:
        logger.warning(f"Не удалось подтвердить удаление {config_name!r}: {e}")


async def execute_ws_command(host: str, command_type: str, args: dict,
                             file_content: str | None = None) -> str:
    """
    Выполняет команду ESPHome WebSocket API.
    
    Если file_content задан — перед выполнением команды создаётся временный конфиг
    в ESPHome через devices/create, после выполнения он удаляется (через devices/delete),
    даже при ошибке (try/finally).
    """
    url = f"ws://{host}:6052/ws"
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

            # --- Если передан абсолютный путь: создаём временный конфиг ---
            tmp_config_name: str | None = None
            if file_content is not None:
                try:
                    tmp_config_name = await create_temp_config(ws, file_content)
                except Exception as e:
                    return f"Ошибка создания временного конфига: {e}"
                # Подменяем имя конфига в args на временное
                args = dict(args)
                args["configuration"] = tmp_config_name

            try:
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

            finally:
                # --- Гарантированно удаляем временный конфиг ---
                if tmp_config_name is not None:
                    await delete_config(ws, tmp_config_name)
                            
    except Exception as e:
        error_msg = f"Критическая ошибка соединения с Device Builder API ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg

# ==========================================
# ИНСТРУМЕНТЫ ДЛЯ АГЕНТА (TOOLS)
# ==========================================

@mcp.tool()
async def validate_yaml(configuration: str, host: str = "localhost") -> str:
    """
    Инструмент 1: Только валидация YAML конфигурации.
    
    Параметр configuration принимает:
      - имя файла: "mcp-test.yaml"
      - относительный путь с префиксом: "config/mcp-test.yaml"
      - абсолютный путь: "/Users/user/project/mcp-test.yaml"
    """
    config_name, file_content = resolve_configuration(configuration)
    return await execute_ws_command(host, "devices/validate",
                                    {"configuration": config_name}, file_content)

@mcp.tool()
async def compile_firmware(configuration: str, host: str = "localhost") -> str:
    """
    Инструмент 2: Только компиляция прошивки без загрузки.
    
    Параметр configuration принимает:
      - имя файла: "mcp-test.yaml"
      - относительный путь с префиксом: "config/mcp-test.yaml"
      - абсолютный путь: "/Users/user/project/mcp-test.yaml"
    """
    config_name, file_content = resolve_configuration(configuration)
    return await execute_ws_command(host, "firmware/compile",
                                    {"configuration": config_name}, file_content)

@mcp.tool()
async def flash_ota(configuration: str, host: str = "localhost") -> str:
    """
    Инструмент 3: Только OTA-прошивка готового бинарника.
    
    Параметр configuration принимает:
      - имя файла: "mcp-test.yaml"
      - относительный путь с префиксом: "config/mcp-test.yaml"
      - абсолютный путь: "/Users/user/project/mcp-test.yaml"
    """
    config_name, file_content = resolve_configuration(configuration)
    return await execute_ws_command(host, "firmware/upload",
                                    {"configuration": config_name, "port": "OTA"}, file_content)

@mcp.tool()
async def compile_and_flash(configuration: str, host: str = "localhost") -> str:
    """
    Инструмент 4: Полный цикл (Компиляция + OTA-Прошивка).
    
    Параметр configuration принимает:
      - имя файла: "mcp-test.yaml"
      - относительный путь с префиксом: "config/mcp-test.yaml"
      - абсолютный путь: "/Users/user/project/mcp-test.yaml"
    """
    config_name, file_content = resolve_configuration(configuration)
    return await execute_ws_command(host, "firmware/install",
                                    {"configuration": config_name, "port": "OTA"}, file_content)

# ==========================================
# ФАЗА P0: ИНСТРУМЕНТЫ МОНИТОРИНГА И ОТЛАДКИ
# ==========================================

@mcp.tool()
async def list_devices(host: str = "localhost") -> str:
    """
    Инструмент 5 (P0): Получение полного списка всех устройств ESPHome и их статусов.
    
    Возвращает список устройств с их именем, файлом конфигурации, статусом (online/offline),
    IP-адресом, версией ESPHome, флагом незакомпилированных изменений (has_pending_changes) и метками.
    """
    url = f"ws://{host}:6052/ws"
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
                devices = result.get("devices", []) if isinstance(result, dict) else result
                
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
                               lines_count: int = 50, host: str = "localhost") -> str:
    """
    Инструмент 6 (P0): Чтение и вывод логов работы устройства в реальном времени.
    
    Параметры:
      - configuration: имя файла, относительный путь (config/...) или абсолютный путь к YAML
      - port: "OTA" (по умолчанию) или serial-порт (/dev/ttyUSB0, COM3 и т.д.)
      - duration_seconds: продолжительность сбора логов в секундах (1-60, по умолчанию 10)
      - lines_count: максимальное количество собираемых строк (по умолчанию 50)
    """
    config_name, file_content = resolve_configuration(configuration)
    duration_seconds = max(1, min(60, duration_seconds))
    lines_count = max(1, min(500, lines_count))
    
    url = f"ws://{host}:6052/ws"
    msg_id = "logs_stream_1"
    output_log = []
    
    logger.info(f"Подключение к {url} для чтения логов {config_name} (порт: {port}, время: {duration_seconds}s)...")
    
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."
            
            tmp_config_name: str | None = None
            if file_content is not None:
                try:
                    tmp_config_name = await create_temp_config(ws, file_content)
                except Exception as e:
                    return f"Ошибка создания временного конфига: {e}"
                target_config = tmp_config_name
            else:
                target_config = config_name
                
            try:
                await ws.send(json.dumps({
                    "command": "devices/logs",
                    "message_id": msg_id,
                    "args": {"configuration": target_config, "port": port}
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
                    return f"Логи устройства ({target_config}) не получены за {duration_seconds} сек. (Устройство оффлайн или тишина в порте)."
                    
                tail = "\n".join(output_log[-lines_count:])
                return f"Собрано {len(output_log)} строк лога ({target_config}, {port}):\n\n{tail}"
                
            finally:
                if tmp_config_name is not None:
                    await delete_config(ws, tmp_config_name)
                    
    except Exception as e:
        error_msg = f"Ошибка чтения логов ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def decode_crash_backtrace(configuration: str, lines: list[str], host: str = "localhost") -> str:
    """
    Инструмент 7 (P0): Расшифровка C++ дампов паники/стектрейсов устройства (Backtrace decoder).
    
    Параметры:
      - configuration: имя файла, относительный путь (config/...) или абсолютный путь к YAML
      - lines: массив строк лога, содержащих дампы паники (например ["Backtrace: 0x400d1234:0x3ffb1234 ..."])
    """
    config_name, file_content = resolve_configuration(configuration)
    url = f"ws://{host}:6052/ws"
    msg_id = "decode_bt_1"
    
    logger.info(f"Отправка дампа стека {config_name} для расшифровки...")
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            first_msg = json.loads(first_msg_raw)
            if first_msg.get("requires_auth"):
                return "Ошибка: ESPHome требует авторизацию, но MCP-сервер пока не поддерживает передачу паролей (requires_auth=true)."
                
            tmp_config_name: str | None = None
            if file_content is not None:
                try:
                    tmp_config_name = await create_temp_config(ws, file_content)
                except Exception as e:
                    return f"Ошибка создания временного конфига: {e}"
                target_config = tmp_config_name
            else:
                target_config = config_name
                
            try:
                await ws.send(json.dumps({
                    "command": "devices/decode_backtrace",
                    "message_id": msg_id,
                    "args": {"configuration": target_config, "lines": lines}
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
                    
                    output = [f"### Результат расшифровки дампа паники ({target_config}):\n"]
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
                    
            finally:
                if tmp_config_name is not None:
                    await delete_config(ws, tmp_config_name)
                    
    except Exception as e:
        error_msg = f"Ошибка расшифровки стектрейса ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def search_yaml_configs(query: str, context_lines: int = 2, case_sensitive: bool = False, host: str = "localhost") -> str:
    """
    Инструмент 8 (P0): Поиск подстроки по всем YAML-конфигурациям ESPHome.
    
    Параметры:
      - query: поисковая подстрока (например "sensor", "GPIO4", "i2c", "wifi")
      - context_lines: количество контекстных строк до и после совпадения (0-10, по умолчанию 2)
      - case_sensitive: учитывать ли регистр символов (по умолчанию False)
    """
    url = f"ws://{host}:6052/ws"
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
    allow_wipe: bool = False,
    host: str = "localhost"
) -> str:
    """
    Инструмент 9 (P1): Управление конфигурацией устройств ESPHome через API.

    Параметр action поддерживает следующие действия:
      - "get"    — прочитать содержимое YAML-конфигурации устройства
      - "update" — записать новое содержимое YAML-конфигурации (требует content)
      - "create" — создать новую конфигурацию устройства (требует content)
      - "rename" — переименовать конфигурацию (требует new_name; config_only=True — без прошивки)
      - "delete" — удалить конфигурацию и связанные файлы

    Параметр configuration — имя файла, например "test.yaml".
    """
    url = f"ws://{host}:6052/ws"
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

    # Формируем аргументы по действию
    if action == "get":
        args = {"configuration": configuration}
    elif action == "update":
        if not content:
            return "Ошибка: для действия 'update' необходимо указать параметр content."
        args = {"configuration": configuration, "content": content, "allow_wipe": allow_wipe}
    elif action == "create":
        if not content:
            return "Ошибка: для действия 'create' необходимо указать параметр content."
        # Убираем расширение .yaml из имени, т.к. devices/create принимает имя без расширения
        name = configuration.removesuffix(".yaml")
        args = {"name": name, "file_content": content, "overwrite": True}
    elif action == "rename":
        if not new_name:
            return "Ошибка: для действия 'rename' необходимо указать параметр new_name."
        args = {"configuration": configuration, "new_name": new_name.removesuffix(".yaml"), "config_only": True}
    elif action == "delete":
        args = {"configuration": configuration}

    logger.info(f"manage_device_config: action={action!r}, configuration={configuration!r}")
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
                        return f"Файл {configuration!r} пустой или не найден."
                    return f"### Содержимое конфигурации `{configuration}`:\n\n```yaml\n{result}\n```"

                elif action == "update":
                    return f"✅ Конфигурация `{configuration}` успешно обновлена."

                elif action == "create":
                    real_name = (result or {}).get("configuration", f"{args['name']}.yaml")
                    return f"✅ Конфигурация создана: `{real_name}`"

                elif action == "rename":
                    new_cfg = (result or {}).get("configuration", "N/A")
                    return f"✅ Конфигурация переименована: `{configuration}` → `{new_cfg}`"

                elif action == "delete":
                    return f"✅ Конфигурация `{configuration}` и связанные файлы удалены."

    except Exception as e:
        error_msg = f"Критическая ошибка manage_device_config ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg


@mcp.tool()
async def get_board_info(
    action: str = "list",
    board_id: str = "",
    platform: str = "",
    query: str = "",
    limit: int = 20,
    host: str = "localhost"
) -> str:
    """
    Инструмент 10 (P1): Получение информации о платах из каталога ESPHome.

    Параметр action поддерживает следующие действия:
      - "list"        — поиск/список плат (с фильтрами query, platform, limit)
      - "get"         — получить полную информацию о плате (требует board_id)
      - "compatible"  — список совместимых/взаимозаменяемых плат (требует board_id)

    Параметры:
      - board_id:  ID платы, например "esp32dev", "nodemcuv2", "lolin32"
      - platform:  фильтр по платформе: "esp32", "esp8266", "rp2040", "nrf52840"
      - query:     строка поиска (название платы, MCU, производитель)
      - limit:     максимальное количество результатов (по умолчанию 20)
    """
    url = f"ws://{host}:6052/ws"
    msg_id = "board_info_1"
    action = action.strip().lower()

    if action not in ("list", "get", "compatible"):
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: list, get, compatible."

    if action in ("get", "compatible") and not board_id:
        return f"Ошибка: для действия '{action}' необходимо указать параметр board_id."

    if action == "list":
        command = "boards/get_boards"
        args: dict = {"limit": limit}
        if query:
            args["query"] = query
        if platform:
            args["platform"] = platform
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
    host: str = "localhost"
) -> str:
    """
    Инструмент 11 (P1): Управление очередью компиляции и сборки устройств ESPHome.

    Параметр action поддерживает следующие действия:
      - "list"        — список задач (с фильтрацией по status_filter и/или configuration)
      - "get"         — получить полную информацию о задаче (требует job_id)
      - "cancel"      — отменить задачу (требует job_id)
      - "clean"       — очистить каталог сборки конкретного устройства (требует configuration)
      - "reset_env"   — глобальный сброс .esphome/ (PlatformIO, external_components, build)

    Параметры:
      - configuration:  имя YAML-файла устройства (например "test.yaml")
      - job_id:         идентификатор задачи (для get, cancel)
      - status_filter:  фильтр по статусу: "queued", "running", "completed", "failed", "cancelled"
    """
    url = f"ws://{host}:6052/ws"
    msg_id = "build_jobs_1"
    action = action.strip().lower()

    valid_actions = ("list", "get", "cancel", "clean", "reset_env")
    if action not in valid_actions:
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: {', '.join(valid_actions)}."

    if action == "get" and not job_id:
        return "Ошибка: для действия 'get' необходимо указать параметр job_id."
    if action == "cancel" and not job_id:
        return "Ошибка: для действия 'cancel' необходимо указать параметр job_id."
    if action == "clean" and not configuration:
        return "Ошибка: для действия 'clean' необходимо указать параметр configuration."

    if action == "list":
        command = "firmware/get_jobs"
        args: dict = {}
        if status_filter:
            args["status"] = status_filter
        if configuration:
            args["configuration"] = configuration
    elif action == "get":
        command = "firmware/get_job"
        args = {"job_id": job_id}
    elif action == "cancel":
        command = "firmware/cancel"
        args = {"job_id": job_id}
    elif action == "clean":
        command = "firmware/clean"
        args = {"configuration": configuration}
    elif action == "reset_env":
        command = "firmware/reset_build_env"
        args = {}

    logger.info(f"manage_build_jobs: action={action!r}, configuration={configuration!r}, job_id={job_id!r}")
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
    host: str = "localhost"
) -> str:
    """
    Инструмент 12 (P2): Пакетная компиляция и/или прошивка нескольких устройств ESPHome.

    Параметр action поддерживает:
      - "compile" — пакетная компиляция без прошивки (firmware/compile_bulk)
      - "install" — пакетная компиляция + OTA-прошивка (firmware/install_bulk).
                    Для оффлайн-устройств обновление откладывается и применяется при следующем включении.

    Параметры:
      - configurations: список YAML-файлов устройств (например ["test.yaml", "ina226.yaml"])
      - port: "OTA" (по умолчанию) или serial-порт для прошивки
    """
    if not configurations:
        return "Ошибка: список configurations пустой."
    action = action.strip().lower()
    if action not in ("compile", "install"):
        return f"Ошибка: неизвестное действие '{action}'. Допустимые: compile, install."

    command = "firmware/compile_bulk" if action == "compile" else "firmware/install_bulk"
    args: dict = {"configurations": configurations}
    if action == "install":
        args["port"] = port

    url = f"ws://{host}:6052/ws"
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
    host: str = "localhost"
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

    url = f"ws://{host}:6052/ws"
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
    host: str = "localhost"
) -> str:
    """
    Инструмент 14 (P2): Управление метками (тегами) устройств ESPHome.

    Заменяет текущий набор меток устройства на указанный.
    Передайте пустой список [] для удаления всех меток.

    Параметры:
      - configuration: имя YAML-файла устройства (например "test.yaml")
      - label_ids:     список идентификаторов меток для установки (например ["room:living_room"])
    """
    url = f"ws://{host}:6052/ws"
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
async def authenticate_esphome(
    username: str = "",
    password: str = "",
    token: str = "",
    host: str = "localhost"
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

    url = f"ws://{host}:6052/ws"
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
                return f"ℹ️ ESPHome на {host}:6052 не требует аутентификации (requires_auth=false). Вход не нужен."

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
                    f"✅ Аутентификация успешна на {host}:6052\n"
                    f"- **Токен:** `{new_token}`\n"
                    f"- **Действителен до:** {expires_at}\n\n"
                    f"💡 Сохраните токен для повторного входа без пароля (параметр token)."
                )

    except Exception as e:
        error_msg = f"Критическая ошибка authenticate_esphome ({url}): {str(e)}"
        logger.error(error_msg)
        return error_msg

if __name__ == "__main__":
    mcp.run(transport='stdio')

