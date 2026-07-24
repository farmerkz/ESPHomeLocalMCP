# Инструкции агента — ESPHome Local MCP Server

## Обязательная документация

При любой модификации `server.py` или логики взаимодействия с ESPHome Device Builder API **обязательно** обратись к официальной документации API перед внесением изменений:

**📖 ESPHome Device Builder API Reference:**
`https://github.com/esphome/device-builder/blob/main/docs/API.md`

Документация описывает:
- Все доступные WebSocket-команды (`devices/*`, `firmware/*`, `boards/*` и др.)
- Форматы запросов (`CommandMessage`) и ответов (`ResultMessage`, `EventMessage`, `ErrorMessage`)
- Коды ошибок (`ErrorCode`) и их значения
- Поведение стриминговых команд (streaming output → result)
- Двухшаговые цепочки задач (`firmware/install` = compile job + upload job)
- Аутентификацию (`requires_auth`, `auth/login`)

## Архитектура сервера

- **Транспорт:** stdio (MCP Protocol)
- **Связь с ESPHome:** WebSocket `ws://<host>:6052/ws`
- **Фреймворк:** `mcp.server.fastmcp.FastMCP`
- **Логирование:** только в `stderr` — `stdout` зарезервирован для MCP-протокола

## Правила работы с путями (`configuration`)

Все 4 инструмента (`validate_yaml`, `compile_firmware`, `flash_ota`, `compile_and_flash`) поддерживают три формата параметра `configuration`:

| Формат | Пример | Поведение |
|--------|--------|-----------|
| Имя файла | `mcp-test.yaml` | Передаётся напрямую в ESPHome API |
| Относительный с префиксом | `config/mcp-test.yaml` | Префикс `config/` обрезается автоматически |
| Абсолютный путь | `/Users/user/project/mcp-test.yaml` | Файл читается с диска, создаётся временный конфиг в ESPHome через `devices/create`, гарантированно удаляется через `devices/delete` после выполнения (`try/finally`) |

## Ключевые особенности ESPHome API

- `devices/validate` — стриминговая команда: ожидай события `output` и `result`
- `firmware/compile`, `firmware/upload`, `firmware/install` — двухшаговые: сначала получи `job_id`, затем подпишись на `firmware/follow_job`
- `devices/create` возвращает **реальное** имя созданного файла в `result.configuration` — ESPHome может slugify имя (убирать спецсимволы), всегда используй значение из ответа API, а не то, что передавалось
- При `requires_auth: true` в `ServerInfoMessage` необходима аутентификация через `auth/login` перед любыми командами
