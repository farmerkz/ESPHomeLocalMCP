import unittest
import asyncio
import json
import os
import re
import sys

# Добавляем родительскую директорию в sys.path для импорта server
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from __version__ import __version__, __version_info__
from server import (
    resolve_configuration,
    validate_yaml,
    list_devices,
    search_yaml_configs,
    get_board_info,
    manage_device_config,
    compile_firmware,
    flash_ota,
    get_server_version
)

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "test_devices.json")
CHANGELOG_PATH = os.path.join(os.path.dirname(__file__), "..", "CHANGELOG.md")

class TestMCPServerUnit(unittest.TestCase):
    """
    Модульные тесты внутренней логики MCP-сервера.
    """

    def test_version_semver_format(self):
        """Проверка соответствия __version__ стандарту SemVer 2.0.0."""
        semver_regex = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[a-zA-Z0-9.-]+)?(?:\+[a-zA-Z0-9.-]+)?$"
        self.assertRegex(__version__, semver_regex, f"__version__ '{__version__}' не соответствует SemVer")
        self.assertIsInstance(__version_info__, tuple)
        self.assertGreaterEqual(len(__version_info__), 3)

    def test_version_in_changelog(self):
        """Проверка наличия текущей версии в CHANGELOG.md."""
        self.assertTrue(os.path.isfile(CHANGELOG_PATH), "Файл CHANGELOG.md не найден")
        with open(CHANGELOG_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn(f"[{__version__}]", content, f"Версия {__version__} не найдена в CHANGELOG.md")

    def test_resolve_configuration_simple(self):
        self.assertEqual(resolve_configuration("test.yaml"), "test.yaml")
        self.assertEqual(resolve_configuration("my_device"), "my_device")

    def test_resolve_configuration_prefix(self):
        self.assertEqual(resolve_configuration("config/test.yaml"), "test.yaml")
        self.assertEqual(resolve_configuration("config/sub/device.yaml"), "sub/device.yaml")

    def test_resolve_configuration_no_disk_read(self):
        # Проверяем, что передачи абсолютных или неизвестных путей возвращаются как строки без обращения к файловой системе
        dummy_path = "/non_existent_directory_12345/dummy.yaml"
        res = resolve_configuration(dummy_path)
        self.assertEqual(res, dummy_path)

    def test_get_server_version_output(self):
        """Тест вызова инструмента get_server_version."""
        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(get_server_version())
            self.assertIn(f"v{__version__}", res)
            self.assertIn("Semantic Versioning 2.0.0", res)
            self.assertIn("16", res)
        finally:
            loop.close()


class TestMCPServerIntegration(unittest.IsolatedAsyncioTestCase):
    """
    Интеграционные тесты работы MCP-сервера с ESPHome API.
    Адаптивно использует манифест test/test_devices.json при наличии.
    """

    def setUp(self):
        self.manifest = None
        self.safe_mode = True

        if os.path.isfile(MANIFEST_PATH):
            try:
                with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "devices" in data:
                        self.manifest = data
                        self.safe_mode = False
            except Exception as e:
                print(f"[TEST WARNING] Ошибка парсинга {MANIFEST_PATH}: {e}. Переход в Safe Mode.")
        else:
            print(f"[TEST WARNING] Файл манифеста {MANIFEST_PATH} не найден. Переход в Safe Mode.")

    async def test_safe_mode_or_full_pipeline(self):
        """
        Тестирование работы с ESPHome API.
        """
        print(f"\n--- Запуск тестов MCP-сервера (Safe Mode: {self.safe_mode}) ---")

        # 1. Проверка получения списка устройств (Read-only)
        list_res = await list_devices()
        self.assertIsInstance(list_res, str)
        print(f"✅ list_devices: {list_res[:100]}...")

        # 2. Проверка поиска по YAML (Read-only)
        search_res = await search_yaml_configs(query="esphome", context_lines=1)
        self.assertIsInstance(search_res, str)
        print(f"✅ search_yaml_configs: {search_res[:100]}...")

        # 3. Проверка информации о плате (Read-only)
        board_res = await get_board_info(action="get", board_id="esp32dev")
        self.assertIsInstance(board_res, str)
        print(f"✅ get_board_info: {board_res[:100]}...")

        if self.safe_mode or not self.manifest:
            print("ℹ️ Безопасный режим (Safe Mode): выполнены только read-only тесты, не меняющие состояние устройств.")
            return

        # Запуск тестов на основе манифеста устройств
        devices = self.manifest.get("devices", [])
        self.assertGreater(len(devices), 0, "Манифест устройств не должен быть пустым.")

        for dev in devices:
            cfg = dev.get("configuration")
            desc = dev.get("description", "")
            allow_compile = dev.get("allow_compile", False)
            allow_ota = dev.get("allow_ota_flash", False)

            print(f"\n📱 Тестирование устройства из манифеста: `{cfg}` ({desc})")

            # Валидация YAML
            val_res = await validate_yaml(cfg)
            self.assertIsInstance(val_res, str)
            print(f"  - validate_yaml ({cfg}): {val_res[:120]}...")

            # Чтение конфигурации через manage_device_config (action=get)
            get_cfg_res = await manage_device_config(action="get", configuration=cfg)
            self.assertIsInstance(get_cfg_res, str)
            print(f"  - manage_device_config (get, {cfg}): {get_cfg_res[:120]}...")

            # Компиляция при наличии флага allow_compile
            if allow_compile:
                print(f"  - Запуск компиляции firmware ({cfg})...")
                compile_res = await compile_firmware(cfg)
                self.assertIsInstance(compile_res, str)
                print(f"  - compile_firmware ({cfg}): {compile_res[:150]}...")

            # OTA-прошивка при наличии флага allow_ota_flash
            if allow_ota:
                print(f"  - Запуск OTA прошивки ({cfg})...")
                ota_res = await flash_ota(cfg)
                self.assertIsInstance(ota_res, str)
                print(f"  - flash_ota ({cfg}): {ota_res[:150]}...")


if __name__ == "__main__":
    unittest.main()
