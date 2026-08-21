import unittest
import asyncio
import json
import os
import re
import sys
import uuid

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
    manage_build_jobs,
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

    def test_tool_signatures_defaults(self):
        """Проверка сигнатур и значений по умолчанию расширенных инструментов прошивки и конфигураций."""
        import inspect
        from server import (
            compile_firmware,
            flash_ota,
            compile_and_flash,
            batch_compile_and_flash,
            manage_device_config,
            get_board_info,
            manage_build_jobs
        )

        sig_compile = inspect.signature(compile_firmware)
        self.assertIn("force_local", sig_compile.parameters)
        self.assertFalse(sig_compile.parameters["force_local"].default)

        sig_flash = inspect.signature(flash_ota)
        self.assertIn("port", sig_flash.parameters)
        self.assertEqual(sig_flash.parameters["port"].default, "OTA")
        self.assertIn("bootloader", sig_flash.parameters)
        self.assertFalse(sig_flash.parameters["bootloader"].default)
        self.assertIn("api_port", sig_flash.parameters)

        sig_candf = inspect.signature(compile_and_flash)
        self.assertIn("port", sig_candf.parameters)
        self.assertEqual(sig_candf.parameters["port"].default, "OTA")
        self.assertIn("force_local", sig_candf.parameters)
        self.assertFalse(sig_candf.parameters["force_local"].default)
        self.assertIn("bootloader", sig_candf.parameters)
        self.assertFalse(sig_candf.parameters["bootloader"].default)
        self.assertIn("api_port", sig_candf.parameters)

        sig_batch = inspect.signature(batch_compile_and_flash)
        self.assertIn("port", sig_batch.parameters)
        self.assertEqual(sig_batch.parameters["port"].default, "OTA")
        self.assertIn("force_local", sig_batch.parameters)
        self.assertIn("bootloader", sig_batch.parameters)
        self.assertIn("api_port", sig_batch.parameters)

        sig_manage = inspect.signature(manage_device_config)
        self.assertIn("board_id", sig_manage.parameters)
        self.assertEqual(sig_manage.parameters["board_id"].default, "")
        self.assertIn("friendly_name", sig_manage.parameters)
        self.assertIn("ssid", sig_manage.parameters)
        self.assertIn("psk", sig_manage.parameters)
        self.assertIn("config_only", sig_manage.parameters)
        self.assertTrue(sig_manage.parameters["config_only"].default)
        self.assertIn("overwrite", sig_manage.parameters)
        self.assertTrue(sig_manage.parameters["overwrite"].default)

        sig_board = inspect.signature(get_board_info)
        self.assertIn("variant", sig_board.parameters)
        self.assertEqual(sig_board.parameters["variant"].default, "")
        self.assertIn("mcu", sig_board.parameters)
        self.assertEqual(sig_board.parameters["mcu"].default, "")
        self.assertIn("tag", sig_board.parameters)
        self.assertEqual(sig_board.parameters["tag"].default, "")
        self.assertIn("offset", sig_board.parameters)
        self.assertEqual(sig_board.parameters["offset"].default, 0)
        self.assertIn("limit", sig_board.parameters)
        self.assertEqual(sig_board.parameters["limit"].default, 20)

        sig_jobs = inspect.signature(manage_build_jobs)
        self.assertIn("configuration", sig_jobs.parameters)
        self.assertEqual(sig_jobs.parameters["configuration"].default, "")
        self.assertIn("job_id", sig_jobs.parameters)
        self.assertEqual(sig_jobs.parameters["job_id"].default, "")
        self.assertIn("status_filter", sig_jobs.parameters)
        self.assertEqual(sig_jobs.parameters["status_filter"].default, "")

    def test_manage_device_config_validation(self):
        """Проверка локальной валидации аргументов в manage_device_config."""
        loop = asyncio.new_event_loop()
        try:
            # 1. Неизвестное действие
            res_unknown = loop.run_until_complete(manage_device_config(action="unknown_action", configuration="test.yaml"))
            self.assertIn("неизвестное действие", res_unknown)

            # 2. Update без content
            res_no_content = loop.run_until_complete(manage_device_config(action="update", configuration="test.yaml"))
            self.assertIn("необходимо указать параметр content", res_no_content)

            # 3. Create без content и без board_id
            res_no_create = loop.run_until_complete(manage_device_config(action="create", configuration="test.yaml"))
            self.assertIn("необходимо указать либо параметр content", res_no_create)

            # 4. Rename без new_name
            res_no_rename = loop.run_until_complete(manage_device_config(action="rename", configuration="test.yaml"))
            self.assertIn("необходимо указать параметр new_name", res_no_rename)
        finally:
            loop.close()

    def test_manage_build_jobs_validation(self):
        """Проверка локальной валидации аргументов в manage_build_jobs."""
        loop = asyncio.new_event_loop()
        try:
            # 1. Неизвестное действие
            res_unknown = loop.run_until_complete(manage_build_jobs(action="unknown_action"))
            self.assertIn("неизвестное действие", res_unknown)

            # 2. Get без job_id
            res_no_jid = loop.run_until_complete(manage_build_jobs(action="get"))
            self.assertIn("необходимо указать параметр job_id", res_no_jid)

            # 3. Cancel без job_id
            res_no_cancel_jid = loop.run_until_complete(manage_build_jobs(action="cancel"))
            self.assertIn("необходимо указать параметр job_id", res_no_cancel_jid)

            # 4. Clean без configuration
            res_no_cfg = loop.run_until_complete(manage_build_jobs(action="clean"))
            self.assertIn("необходимо указать параметр configuration", res_no_cfg)

            # 5. Clear_queued без configuration
            res_no_queued_cfg = loop.run_until_complete(manage_build_jobs(action="clear_queued"))
            self.assertIn("необходимо указать параметр configuration", res_no_queued_cfg)
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
                print(f"  - Запуск компиляции firmware ({cfg}, force_local=False)...")
                compile_res = await compile_firmware(cfg, force_local=False)
                self.assertIsInstance(compile_res, str)
                print(f"  - compile_firmware ({cfg}): {compile_res[:150]}...")

            # OTA-прошивка при наличии флага allow_ota_flash
            if allow_ota:
                print(f"  - Запуск OTA прошивки ({cfg}, port='OTA', bootloader=False)...")
                ota_res = await flash_ota(cfg, port="OTA", bootloader=False)
                self.assertIsInstance(ota_res, str)
                print(f"  - flash_ota ({cfg}): {ota_res[:150]}...")

    async def test_device_config_crud_lifecycle(self):
        """
        Изолированное тестирование полного жизненного цикла управления конфигурацией (CRUD + Template + Rename).
        Использует уникальный временный идентификатор с гарантированной очисткой (Teardown Guard).
        """
        uid = uuid.uuid4().hex[:6]
        fixture_name = f"mcp_fix_{uid}"
        renamed_fixture_name = f"mcp_rn_{uid}"
        cfg_file = f"{fixture_name}.yaml"
        renamed_cfg_file = f"{renamed_fixture_name}.yaml"

        print(f"\n🔄 Запуск теста жизненного цикла конфигурации: `{cfg_file}`")
        try:
            # 1. Создание по шаблону платы (board_id) без передачи content
            create_res = await manage_device_config(
                action="create",
                configuration=cfg_file,
                board_id="esp32dev",
                friendly_name="MCP Temporary Test Fixture",
                overwrite=True
            )
            self.assertIn("✅ Конфигурация создана:", create_res)
            print(f"  - create (board_id='esp32dev'): {create_res}")

            # 2. Чтение созданной конфигурации и проверка шаблона
            get_res = await manage_device_config(action="get", configuration=cfg_file)
            self.assertIn("esp32", get_res)
            self.assertIn("esp32dev", get_res)
            print(f"  - get ({cfg_file}): прочитано {len(get_res)} байт")

            # 3. Валидация сгенерированного шаблона через ESPHome API
            val_res = await validate_yaml(cfg_file)
            self.assertIn("УСПЕШНО", val_res)
            print(f"  - validate_yaml ({cfg_file}): шаблон валиден")

            # 4. Обновление содержимого (update)
            update_content = (
                f"# MCP Automated Test Fixture\n"
                f"esphome:\n"
                f"  name: {fixture_name}\n"
                f"  friendly_name: \"Updated Test Fixture\"\n\n"
                f"esp32:\n"
                f"  board: esp32dev\n\n"
                f"sensor:\n"
                f"  - platform: uptime\n"
                f"    name: \"Uptime Sensor\"\n"
            )
            update_res = await manage_device_config(action="update", configuration=cfg_file, content=update_content)
            self.assertIn("успешно обновлена", update_res)
            print(f"  - update ({cfg_file}): {update_res}")

            # 5. Офлайн-переименование (rename, config_only=True)
            rename_res = await manage_device_config(
                action="rename",
                configuration=cfg_file,
                new_name=renamed_cfg_file,
                config_only=True
            )
            self.assertIn("успешно переименована", rename_res)
            print(f"  - rename ({cfg_file} -> {renamed_cfg_file}): {rename_res}")

            # 6. Проверка, что переименованный файл существует и читается
            get_renamed_res = await manage_device_config(action="get", configuration=renamed_cfg_file)
            self.assertIn("Updated Test Fixture", get_renamed_res)
            print(f"  - get ({renamed_cfg_file}): подтверждено существование нового файла")

        finally:
            # Teardown Guard: гарантированное удаление всех временных файлов
            print(f"  - Teardown: удаление временных файлов `{cfg_file}` и `{renamed_cfg_file}`...")
            await manage_device_config(action="delete", configuration=cfg_file)
            await manage_device_config(action="delete", configuration=renamed_cfg_file)
            print("  ✅ Teardown завершен.")

    async def test_get_board_info_extended_filters(self):
        """Тестирование расширенной фильтрации каталога плат (variant, platform, limit, offset)."""
        print("\n🔍 Запуск тестов расширенной фильтрации get_board_info...")

        # 1. Поиск по variant="esp32c3"
        res_c3 = await get_board_info(action="list", platform="esp32", variant="esp32c3", limit=5)
        self.assertIn("Каталог плат ESPHome", res_c3)
        self.assertIn("esp32c3", res_c3.lower())
        print(f"  - get_board_info (variant='esp32c3'): найдено плат")

        # 2. Поиск по platform="rp2040"
        res_rp = await get_board_info(action="list", platform="rp2040", limit=5)
        self.assertIn("Каталог плат ESPHome", res_rp)
        self.assertIn("rp2040", res_rp.lower())
        print(f"  - get_board_info (platform='rp2040'): найдено плат")

        # 3. Пагинация (limit + offset)
        res_page = await get_board_info(action="list", limit=3, offset=3)
        self.assertIn("Каталог плат ESPHome", res_page)
        print(f"  - get_board_info (limit=3, offset=3): пагинация работает")

    async def test_manage_build_jobs_actions(self):
        """Тестирование расширенных действий управления задачами сборки (list, clear, clear_queued)."""
        print("\n⚙️ Запуск тестов действий manage_build_jobs...")

        # 1. Получение списка задач
        list_res = await manage_build_jobs(action="list")
        self.assertTrue("Задачи сборки ESPHome" in list_res or "Задачи в очереди не найдены" in list_res)
        print(f"  - manage_build_jobs (action='list'): {list_res[:100]}...")

        # 2. Очистка истории задач (firmware/clear)
        clear_res = await manage_build_jobs(action="clear")
        self.assertIn("успешно очищена", clear_res)
        print(f"  - manage_build_jobs (action='clear'): {clear_res}")

        # 3. Сброс отложенного обновления устройства (firmware/clear_queued_update)
        queued_res = await manage_build_jobs(action="clear_queued", configuration="test.yaml")
        self.assertIn("успешно сброшено", queued_res)
        print(f"  - manage_build_jobs (action='clear_queued'): {queued_res}")


if __name__ == "__main__":
    unittest.main()
