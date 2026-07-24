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

def clean_ansi(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
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

if __name__ == "__main__":
    mcp.run(transport='stdio')
