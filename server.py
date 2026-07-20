from mcp.server.fastmcp import FastMCP
import asyncio
import websockets
import json
import sys
import logging
import re

# 1. ЗАЩИТА ПРОТОКОЛА: Перенаправляем все логи в stderr.
# Обычный print() использовать категорически запрещено!
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("esphome-mcp")

# Инициализация MCP сервера
mcp = FastMCP("esphome-device-builder")

def clean_ansi(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

async def execute_ws_command(host: str, command_type: str, configuration: str) -> str:
    url = f"ws://{host}:6052/ws"
    output_log = []
    request_id = "1"
    
    logger.info(f"Подключение к {url} для выполнения {command_type}...")
    
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            # Сначала нужно пропустить ServerInfoMessage, если он есть
            first_msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            
            # Если команда валидации, формат простой
            if command_type == "devices/validate":
                await ws.send(json.dumps({
                    "command": command_type,
                    "message_id": request_id,
                    "args": {"configuration": configuration}
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
                    "args": {"configuration": configuration}
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
async def validate_yaml(configuration: str, host: str = "host.docker.internal") -> str:
    """Инструмент 1: Только валидация YAML конфигурации."""
    if configuration.startswith("config/"): configuration = configuration[7:]
    return await execute_ws_command(host, "devices/validate", configuration)

@mcp.tool()
async def compile_firmware(configuration: str, host: str = "host.docker.internal") -> str:
    """Инструмент 2: Только компиляция прошивки без загрузки."""
    if configuration.startswith("config/"): configuration = configuration[7:]
    return await execute_ws_command(host, "firmware/compile", configuration)

@mcp.tool()
async def flash_ota(configuration: str, host: str = "host.docker.internal") -> str:
    """Инструмент 3: Только OTA-прошивка готового бинарника."""
    if configuration.startswith("config/"): configuration = configuration[7:]
    return await execute_ws_command(host, "firmware/upload", configuration)

@mcp.tool()
async def compile_and_flash(configuration: str, host: str = "host.docker.internal") -> str:
    """Инструмент 4: Полный цикл (Компиляция + OTA-Прошивка)."""
    if configuration.startswith("config/"): configuration = configuration[7:]
    return await execute_ws_command(host, "firmware/install", configuration)

if __name__ == "__main__":
    mcp.run(transport='stdio')
