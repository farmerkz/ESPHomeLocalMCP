# 📖 Руководство пользователя: Подключение инструкций агента в проектах ESPHome

В данном руководстве подробно описано, как подключить файл инструкций [**`docs/ESPHOME_AGENT_GUIDE.md`**](ESPHOME_AGENT_GUIDE.md) к вашим проектам разработки прошивок и управления устройствами ESPHome при работе с различными AI-ассистентами и IDE (**Antigravity, Cursor, VS Code GitHub Copilot, Claude Desktop, Windsurf, Aider** и др.).

---

## 🎯 Зачем подключать инструкции к проекту?

Подключение `ESPHOME_AGENT_GUIDE.md` дает AI-ассистенту:
1. **Знание всех 27 инструментов MCP-сервера** и понимание, когда какой инструмент вызывать.
2. **Безопасные паттерны разработки** (обязательная валидация перед прошивкой, защита `secrets.yaml`, Dry-Run перед изменением автоматизаций).
3. **Автономность в решении задач** (подбор плат и компонентов по базе >940 записей, сборка, расшифровка дампов паники, сетевая диагностика).

---

## 🚀 Варианты подключения по средам разработки

---

### 1. Google Antigravity & Gemini CLI

Antigravity автоматически загружает правила и инструкции из директории `.agents/`.

#### Вариант А: Подключение через правило (Рекомендуется)
Создайте файл `.agents/rules/esphome.md` в корне вашего проекта ESPHome:

```markdown
# Правила работы с ESPHome

При разработке, редактировании конфигураций, компиляции, прошивке и отладке устройств ESPHome строго следуйте инструкциям из руководства:
@[docs/ESPHOME_AGENT_GUIDE.md]
```

#### Вариант Б: Прямое включение в `.agents/AGENTS.md`
В основном файле `.agents/AGENTS.md` добавьте секцию:

```markdown
## 🔌 Интеграция с ESPHome Local MCP Server

Все операции с устройствами ESPHome выполняются через MCP-инструменты.
Подробные сценарии и справочник инструментов: @[docs/ESPHOME_AGENT_GUIDE.md]
```

---

### 2. Cursor IDE

В Cursor инструкции подключаются через глобальные правила проекта или систему `.cursor/rules/`.

#### Вариант А: Модульное правило `.cursor/rules/esphome.mdc` (Рекомендуется для Cursor 0.40+)
Создайте файл `.cursor/rules/esphome.mdc`:

```markdown
---
description: Правила и сценарии разработки ESPHome через MCP сервер
globs: ["*.yaml", "*.yml", "config/**"]
alwaysApply: true
---

# ESPHome Development Instructions

При работе с конфигурациями ESPHome, сборке, прошивке и диагностике используйте инструменты ESPHome Local MCP Server.
Полное руководство по сценариям и инструментам:
@docs/ESPHOME_AGENT_GUIDE.md
```

#### Вариант Б: Корневой файл `.cursorrules`
Создайте или дополните файл `.cursorrules` в корне проекта:

```markdown
# ESPHome MCP Agent Rules
Выступай в роли эксперта по разработке прошивок ESPHome.
Для работы с устройствами используй инструменты ESPHome Local MCP Server.
Обязательно соблюдай правила из файла docs/ESPHOME_AGENT_GUIDE.md:
- Всегда выполняй validate_yaml перед compile_firmware и flash_ota.
- Для редактирования YAML используй manage_device_config или manage_automations.
- Никогда не раскрывай значения секретов из secrets.yaml.
```

---

### 3. VS Code + GitHub Copilot

GitHub Copilot в VS Code поддерживает проектные инструкции через `.github/copilot-instructions.md`.

Создайте файл `.github/copilot-instructions.md`:

```markdown
# Инструкции для GitHub Copilot: ESPHome Project

При работе с устройствами и конфигурациями в этом репозитории используй инструменты ESPHome Local MCP Server:
- Руководство по сценариям и инструментам: [docs/ESPHOME_AGENT_GUIDE.md](docs/ESPHOME_AGENT_GUIDE.md)
- Всегда валидируй конфигурации через `validate_yaml` перед прошивкой.
- При отладке сетевых проблем используй `troubleshoot_device`.
- При расшифровке сбоев C++ используй `decode_crash_backtrace`.
```

---

### 4. Claude Desktop & Claude Code

#### Для Claude Desktop (`claude_desktop_config.json`):
Добавьте сервер в конфигурацию `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) или `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "esphome-local": {
      "command": "/Users/username/Documents/github/ESPHomeLocalMCP/.venv/bin/python",
      "args": ["/Users/username/Documents/github/ESPHomeLocalMCP/server.py"]
    }
  }
}
```

#### Для Claude Code (CLI):
Создайте файл `CLAUDE.md` в корне вашего проекта ESPHome:

```markdown
# ESPHome Project Guide for Claude

- All device management, compilation, flashing, and diagnostics must be performed via `esphome-local` MCP tools.
- Reference guide and playbooks: `docs/ESPHOME_AGENT_GUIDE.md`.
- Never flash devices without running `validate_yaml` first.
```

---

### 5. Windsurf IDE (Codeium Cascade)

Создайте файл `.windsurfrules` в корне проекта:

```markdown
# Windsurf Rules for ESPHome
При работе с устройствами ESPHome руководствуйся инструкциями из файла docs/ESPHOME_AGENT_GUIDE.md.
Используй подключенные MCP-инструменты сервера ESPHome Local:
- Валидация: validate_yaml
- Сборка и прошивка: compile_firmware, flash_ota, compile_and_flash
- Автоматизации: manage_automations (сначала dry-run apply=False)
- Секреты: manage_secrets
```

---

### 6. Aider (AI Pair Programming in Terminal)

Добавьте файл инструкций в конфигурацию `.aider.conf.yml`:

```yaml
read:
  - docs/ESPHOME_AGENT_GUIDE.md
```

Или запускайте Aider с ключом `--read`:

```bash
aider --read docs/ESPHOME_AGENT_GUIDE.md
```

---

## ⚙️ Настройка конфигурации подключения MCP-сервера

Чтобы AI-ассистент имел доступ к инструментам, укажите путь к `server.py` и его виртуальному окружению в файле конфигурации MCP:

### Пример `mcp_config.json` / `settings.json`:

```json
{
  "mcpServers": {
    "esphome-local": {
      "command": "/абсолютный/путь/к/ESPHomeLocalMCP/.venv/bin/python",
      "args": [
        "-u",
        "/абсолютный/путь/к/ESPHomeLocalMCP/server.py"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

> [!TIP]
> Параметр `-u` в Python обеспечивает небуферизованный вывод, гарантируя моментальную передачу JSON-RPC сообщений протокола MCP.

---

## 📁 Рекомендуемая структура проекта ESPHome

```text
my-esphome-project/
├── .agents/
│   └── rules/
│       └── esphome.md              <-- Правило для Antigravity / Gemini CLI
├── .cursor/
│   └── rules/
│       └── esphome.mdc             <-- Правило для Cursor
├── .github/
│   └── copilot-instructions.md     <-- Инструкция для VS Code Copilot
├── docs/
│   ├── ESPHOME_AGENT_GUIDE.md      <-- Скопированный файл инструкций для агента
│   └── USER_INTEGRATION_GUIDE.md   <-- Данная инструкция
├── CLAUDE.md                       <-- Инструкция для Claude Code
├── .cursorrules                    <-- Корневые правила для Cursor
├── .windsurfrules                  <-- Правила для Windsurf
├── livingroom-light.yaml           <-- Конфигурация устройства
├── climate-sensor.yaml             <-- Конфигурация устройства
└── secrets.yaml                    <-- Зашифрованные/приватные секреты
```
