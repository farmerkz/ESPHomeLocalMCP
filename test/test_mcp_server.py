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
    migrate_device_config,
    search_components,
    manage_secrets,
    get_host_info,
    manage_labels,
    batch_manage_devices,
    troubleshoot_device,
    manage_version_history,
    manage_automations,
    get_firmware_binaries,
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

        sig_mig = inspect.signature(migrate_device_config)
        self.assertIn("configuration", sig_mig.parameters)
        self.assertEqual(sig_mig.parameters["configuration"].default, "")
        self.assertIn("content", sig_mig.parameters)
        self.assertEqual(sig_mig.parameters["content"].default, "")
        self.assertIn("apply", sig_mig.parameters)
        self.assertFalse(sig_mig.parameters["apply"].default)

        sig_comp = inspect.signature(search_components)
        self.assertIn("action", sig_comp.parameters)
        self.assertEqual(sig_comp.parameters["action"].default, "search")
        self.assertIn("query", sig_comp.parameters)
        self.assertEqual(sig_comp.parameters["query"].default, "")
        self.assertIn("category", sig_comp.parameters)
        self.assertEqual(sig_comp.parameters["category"].default, "")
        self.assertIn("platform", sig_comp.parameters)
        self.assertEqual(sig_comp.parameters["platform"].default, "")
        self.assertIn("component_id", sig_comp.parameters)
        self.assertEqual(sig_comp.parameters["component_id"].default, "")
        self.assertIn("limit", sig_comp.parameters)
        self.assertEqual(sig_comp.parameters["limit"].default, 20)
        self.assertIn("offset", sig_comp.parameters)
        self.assertEqual(sig_comp.parameters["offset"].default, 0)

        sig_sec = inspect.signature(manage_secrets)
        self.assertIn("action", sig_sec.parameters)
        self.assertEqual(sig_sec.parameters["action"].default, "list")
        self.assertIn("key", sig_sec.parameters)
        self.assertEqual(sig_sec.parameters["key"].default, "")
        self.assertIn("value", sig_sec.parameters)
        self.assertEqual(sig_sec.parameters["value"].default, "")
        self.assertIn("ssid", sig_sec.parameters)
        self.assertEqual(sig_sec.parameters["ssid"].default, "")
        self.assertIn("psk", sig_sec.parameters)
        self.assertEqual(sig_sec.parameters["psk"].default, "")

        sig_host = inspect.signature(get_host_info)
        self.assertIn("action", sig_host.parameters)
        self.assertEqual(sig_host.parameters["action"].default, "version")

        sig_lbl = inspect.signature(manage_labels)
        self.assertIn("action", sig_lbl.parameters)
        self.assertEqual(sig_lbl.parameters["action"].default, "list")
        self.assertIn("label_id", sig_lbl.parameters)
        self.assertEqual(sig_lbl.parameters["label_id"].default, "")
        self.assertIn("name", sig_lbl.parameters)
        self.assertEqual(sig_lbl.parameters["name"].default, "")
        self.assertIn("color", sig_lbl.parameters)
        self.assertEqual(sig_lbl.parameters["color"].default, "")

        sig_bulk = inspect.signature(batch_manage_devices)
        self.assertIn("action", sig_bulk.parameters)
        self.assertEqual(sig_bulk.parameters["action"].default, "archive")
        self.assertIn("configurations", sig_bulk.parameters)
        self.assertEqual(sig_bulk.parameters["configurations"].default, None)
        self.assertIn("label_ids", sig_bulk.parameters)
        self.assertEqual(sig_bulk.parameters["label_ids"].default, None)
        self.assertIn("updates", sig_bulk.parameters)
        self.assertEqual(sig_bulk.parameters["updates"].default, None)

        sig_tb = inspect.signature(troubleshoot_device)
        self.assertIn("configuration", sig_tb.parameters)
        self.assertEqual(sig_tb.parameters["configuration"].default, "")
        self.assertIn("action", sig_tb.parameters)
        self.assertEqual(sig_tb.parameters["action"].default, "probe")

        sig_vh = inspect.signature(manage_version_history)
        self.assertIn("action", sig_vh.parameters)
        self.assertEqual(sig_vh.parameters["action"].default, "log")
        self.assertIn("configuration", sig_vh.parameters)
        self.assertEqual(sig_vh.parameters["configuration"].default, "")
        self.assertIn("sha", sig_vh.parameters)
        self.assertEqual(sig_vh.parameters["sha"].default, "")

        sig_auto = inspect.signature(manage_automations)
        self.assertIn("action", sig_auto.parameters)
        self.assertEqual(sig_auto.parameters["action"].default, "parse")
        self.assertIn("configuration", sig_auto.parameters)
        self.assertEqual(sig_auto.parameters["configuration"].default, "")
        self.assertIn("component_id", sig_auto.parameters)
        self.assertEqual(sig_auto.parameters["component_id"].default, "")
        self.assertIn("trigger", sig_auto.parameters)
        self.assertEqual(sig_auto.parameters["trigger"].default, "")
        self.assertIn("apply", sig_auto.parameters)
        self.assertEqual(sig_auto.parameters["apply"].default, False)

        sig_bin = inspect.signature(get_firmware_binaries)
        self.assertIn("configuration", sig_bin.parameters)
        self.assertIn("action", sig_bin.parameters)
        self.assertEqual(sig_bin.parameters["action"].default, "list")
        self.assertIn("file", sig_bin.parameters)
        self.assertEqual(sig_bin.parameters["file"].default, "")
        self.assertIn("save_path", sig_bin.parameters)
        self.assertEqual(sig_bin.parameters["save_path"].default, "")

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

    def test_migrate_device_config_validation(self):
        """Проверка локальной валидации аргументов в migrate_device_config."""
        loop = asyncio.new_event_loop()
        try:
            # Вызов без configuration и content
            res_no_args = loop.run_until_complete(migrate_device_config())
            self.assertIn("необходимо указать либо параметр 'configuration'", res_no_args)
        finally:
            loop.close()

    def test_search_components_validation(self):
        """Проверка локальной валидации аргументов в search_components."""
        loop = asyncio.new_event_loop()
        try:
            # 1. Неизвестное действие
            res_unknown = loop.run_until_complete(search_components(action="unknown_action"))
            self.assertIn("неизвестное действие", res_unknown)

            # 2. Get без component_id и query
            res_no_id = loop.run_until_complete(search_components(action="get"))
            self.assertIn("необходимо указать параметр 'component_id' или 'query'", res_no_id)
        finally:
            loop.close()

    def test_manage_secrets_validation(self):
        """Проверка локальной валидации аргументов в manage_secrets."""
        loop = asyncio.new_event_loop()
        try:
            # 1. Неизвестное действие
            res_unknown = loop.run_until_complete(manage_secrets(action="unknown_action"))
            self.assertIn("неизвестное действие", res_unknown)

            # 2. Set без key или без value
            res_no_key = loop.run_until_complete(manage_secrets(action="set", value="abc"))
            self.assertIn("необходимо указать оба параметра", res_no_key)

            # 3. Set_wifi без ssid
            res_no_ssid = loop.run_until_complete(manage_secrets(action="set_wifi"))
            self.assertIn("необходимо указать параметр 'ssid'", res_no_ssid)
        finally:
            loop.close()

    def test_get_host_info_validation(self):
        """Проверка локальной валидации аргументов в get_host_info."""
        loop = asyncio.new_event_loop()
        try:
            res_unknown = loop.run_until_complete(get_host_info(action="unknown_action"))
            self.assertIn("неизвестное действие", res_unknown)
        finally:
            loop.close()

    def test_manage_labels_validation(self):
        """Проверка локальной валидации аргументов в manage_labels."""
        loop = asyncio.new_event_loop()
        try:
            # 1. Неизвестное действие
            res_unknown = loop.run_until_complete(manage_labels(action="unknown_action"))
            self.assertIn("неизвестное действие", res_unknown)

            # 2. Create без name
            res_no_name = loop.run_until_complete(manage_labels(action="create"))
            self.assertIn("необходимо указать параметр 'name'", res_no_name)

            # 3. Update без label_id
            res_no_lid = loop.run_until_complete(manage_labels(action="update", name="Tag"))
            self.assertIn("необходимо указать параметр 'label_id'", res_no_lid)

            # 4. Update без name и без color
            res_no_upd = loop.run_until_complete(manage_labels(action="update", label_id="abc"))
            self.assertIn("необходимо указать хотя бы один изменяемый параметр", res_no_upd)

            # 5. Delete без label_id
            res_no_del_lid = loop.run_until_complete(manage_labels(action="delete"))
            self.assertIn("необходимо указать параметр 'label_id'", res_no_del_lid)
        finally:
            loop.close()

    def test_batch_manage_devices_validation(self):
        """Проверка локальной валидации аргументов в batch_manage_devices."""
        loop = asyncio.new_event_loop()
        try:
            # 1. Неизвестное действие
            res_unknown = loop.run_until_complete(batch_manage_devices(action="unknown_action"))
            self.assertIn("неизвестное действие", res_unknown)

            # 2. Archive без configurations
            res_no_arc_cfg = loop.run_until_complete(batch_manage_devices(action="archive"))
            self.assertIn("необходимо передать список 'configurations'", res_no_arc_cfg)

            # 3. Delete без configurations
            res_no_del_cfg = loop.run_until_complete(batch_manage_devices(action="delete"))
            self.assertIn("необходимо передать список 'configurations'", res_no_del_cfg)

            # 4. Set_labels без configurations и без updates
            res_no_lbl_args = loop.run_until_complete(batch_manage_devices(action="set_labels"))
            self.assertIn("необходимо передать либо 'configurations'", res_no_lbl_args)
        finally:
            loop.close()

    def test_troubleshoot_device_validation(self):
        """Проверка локальной валидации аргументов в troubleshoot_device."""
        loop = asyncio.new_event_loop()
        try:
            # 1. Неизвестное действие
            res_unknown = loop.run_until_complete(troubleshoot_device(action="unknown_action"))
            self.assertIn("неизвестное действие", res_unknown)

            # 2. Probe без configuration
            res_no_cfg = loop.run_until_complete(troubleshoot_device(action="probe"))
            self.assertIn("необходимо указать параметр 'configuration'", res_no_cfg)
        finally:
            loop.close()

    def test_manage_version_history_validation(self):
        """Проверка локальной валидации аргументов в manage_version_history."""
        loop = asyncio.new_event_loop()
        try:
            # 1. Неизвестное действие
            res_unknown = loop.run_until_complete(manage_version_history(action="unknown_action"))
            self.assertIn("неизвестное действие", res_unknown)

            # 2. Show без configuration
            res_no_cfg = loop.run_until_complete(manage_version_history(action="show"))
            self.assertIn("необходимо указать параметр 'configuration'", res_no_cfg)

            # 3. Restore без configuration
            res_no_res_cfg = loop.run_until_complete(manage_version_history(action="restore", sha="123456"))
            self.assertIn("необходимо указать параметр 'configuration'", res_no_res_cfg)

            # 4. Restore без sha
            res_no_res_sha = loop.run_until_complete(manage_version_history(action="restore", configuration="test.yaml"))
            self.assertIn("необходимо указать хэш коммита", res_no_res_sha)
        finally:
            loop.close()

    def test_manage_automations_validation(self):
        """Проверка локальной валидации аргументов в manage_automations."""
        loop = asyncio.new_event_loop()
        try:
            # 1. Неизвестное действие
            res_unknown = loop.run_until_complete(manage_automations(action="unknown_action"))
            self.assertIn("неизвестное действие", res_unknown)

            # 2. Parse без configuration
            res_no_parse_cfg = loop.run_until_complete(manage_automations(action="parse"))
            self.assertIn("необходимо указать параметр 'configuration'", res_no_parse_cfg)

            # 3. Upsert без component_id
            res_no_cid = loop.run_until_complete(manage_automations(action="upsert", configuration="test.yaml"))
            self.assertIn("необходимо указать параметр 'component_id'", res_no_cid)

            # 4. Upsert без trigger
            res_no_trig = loop.run_until_complete(manage_automations(action="upsert", configuration="test.yaml", component_id="sw1"))
            self.assertIn("необходимо указать параметр 'trigger'", res_no_trig)

            # 5. Upsert без automation dict
            res_no_auto = loop.run_until_complete(manage_automations(action="upsert", configuration="test.yaml", component_id="sw1", trigger="on_turn_on"))
            self.assertIn("необходимо указать словарь 'automation'", res_no_auto)

            # 6. Delete без trigger
            res_del_no_trig = loop.run_until_complete(manage_automations(action="delete", configuration="test.yaml", component_id="sw1"))
            self.assertIn("необходимо указать параметр 'trigger'", res_del_no_trig)
        finally:
            loop.close()

    def test_get_firmware_binaries_validation(self):
        """Проверка локальной валидации аргументов в get_firmware_binaries."""
        loop = asyncio.new_event_loop()
        try:
            # 1. Без configuration
            res_no_cfg = loop.run_until_complete(get_firmware_binaries(configuration=""))
            self.assertIn("обязателен для работы с артефактами", res_no_cfg)

            # 2. Неизвестное действие
            res_unknown = loop.run_until_complete(get_firmware_binaries(configuration="test.yaml", action="unknown_action"))
            self.assertIn("неизвестное действие", res_unknown)

            # 3. Token без file
            res_no_tok_file = loop.run_until_complete(get_firmware_binaries(configuration="test.yaml", action="token", file=""))
            self.assertIn("необходимо указать параметр 'file'", res_no_tok_file)

            # 4. Download без file
            res_no_down_file = loop.run_until_complete(get_firmware_binaries(configuration="test.yaml", action="download", file=""))
            self.assertIn("необходимо указать параметр 'file'", res_no_down_file)
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
        fixture_name = f"mcp-fix-{uid}"
        renamed_fixture_name = f"mcp-rn-{uid}"
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

    async def test_migrate_device_config_content_and_lifecycle(self):
        """Тестирование автоматической миграции YAML синтаксиса (Dry-Run и Apply Lifecycle)."""
        print("\n🔄 Запуск тестов migrate_device_config...")

        # 1. Проверка актуального YAML (не требует миграции)
        actual_yaml = "esphome:\n  name: test-act\nesp32:\n  board: esp32dev\n"
        res_actual = await migrate_device_config(content=actual_yaml)
        self.assertIn("не требует миграции", res_actual)
        print(f"  - migrate_device_config (актуальный YAML): подтверждено отсутствие необходимости миграции")

        # 2. Dry-Run проверка устаревшего YAML (services -> actions)
        legacy_yaml = (
            "esphome:\n"
            "  name: test-legacy\n\n"
            "esp32:\n"
            "  board: esp32dev\n\n"
            "api:\n"
            "  services:\n"
            "    - service: custom_service\n"
            "      then:\n"
            "        - logger.log: \"Legacy service test\"\n"
        )
        res_legacy_dry = await migrate_device_config(content=legacy_yaml, apply=False)
        self.assertIn("Анализ миграции синтаксиса ESPHome", res_legacy_dry)
        self.assertIn("actions", res_legacy_dry)
        print(f"  - migrate_device_config (Dry-Run): успешно обнаружены устаревшие поля")

        # 3. Сквозной жизненный цикл с применением миграции к файлу устройства (Apply Lifecycle)
        uid = uuid.uuid4().hex[:6]
        fixture_name = f"mcp-mig-{uid}"
        cfg_file = f"{fixture_name}.yaml"

        print(f"  - Запуск Apply Lifecycle миграции для фикстуры `{cfg_file}`...")
        try:
            # Создаем устройство с устаревшим YAML
            fixture_legacy_content = (
                f"esphome:\n"
                f"  name: {fixture_name}\n\n"
                f"esp32:\n"
                f"  board: esp32dev\n\n"
                f"logger:\n\n"
                f"wifi:\n"
                f"  ssid: \"Mock-Wifi\"\n\n"
                f"api:\n"
                f"  services:\n"
                f"    - service: legacy_action\n"
                f"      then:\n"
                f"        - logger.log: \"Hello\"\n"
            )
            create_res = await manage_device_config(
                action="create",
                configuration=cfg_file,
                content=fixture_legacy_content,
                overwrite=True
            )
            self.assertIn("✅ Конфигурация создана:", create_res)

            # Применяем миграцию (apply=True)
            apply_res = await migrate_device_config(configuration=cfg_file, apply=True)
            self.assertIn("успешно применена и сохранена", apply_res)
            print(f"  - migrate_device_config (apply=True): {apply_res[:120]}...")

            # Проверяем, что в файле сохранился мигрированный синтаксис
            updated_content = await manage_device_config(action="get", configuration=cfg_file)
            self.assertIn("actions:", updated_content)
            self.assertIn("action: legacy_action", updated_content)
            self.assertNotIn("services:", updated_content)

            # Валидируем мигрированный файл через ESPHome API
            val_res = await validate_yaml(cfg_file)
            self.assertIn("УСПЕШНО", val_res)
            print(f"  - validate_yaml ({cfg_file}): мигрированный конфиг валиден")

        finally:
            # Teardown Guard
            print(f"  - Teardown: удаление временного файла `{cfg_file}`...")
            await manage_device_config(action="delete", configuration=cfg_file)
            print("  ✅ Teardown завершен.")

    async def test_search_components_search_and_get(self):
        """Тестирование поиска и детальной спецификации компонентов ESPHome."""
        print("\n🔍 Запуск тестов search_components (search & get)...")

        # 1. Поиск по query="bme280"
        search_res = await search_components(action="search", query="bme280")
        self.assertIn("Каталог компонентов ESPHome", search_res)
        self.assertIn("bme280", search_res.lower())
        print(f"  - search_components (query='bme280'): {search_res[:100]}...")

        # 2. Поиск по category="display" с пагинацией
        disp_res = await search_components(action="search", category="display", limit=3, offset=3)
        self.assertIn("Каталог компонентов ESPHome", disp_res)
        print(f"  - search_components (category='display', limit=3, offset=3): пагинация работает")

        # 3. Детальный технический паспорт компонента (action="get")
        get_res = await search_components(action="get", component_id="sensor.bme280_i2c")
        self.assertIn("Компонент ESPHome", get_res)
        self.assertIn("Категория", get_res)
        self.assertIn("i2c", get_res.lower())
        print(f"  - search_components (get 'sensor.bme280_i2c'): {get_res[:120]}...")

    async def test_search_components_categories_and_pin_modes(self):
        """Тестирование справочника категорий и режимов пинов GPIO-расширителей."""
        print("\n📚 Запуск тестов search_components (categories & pin_modes)...")

        # 1. Список категорий компонентов
        cats_res = await search_components(action="categories")
        self.assertIn("Категории компонентов ESPHome", cats_res)
        self.assertIn("sensor", cats_res.lower())
        self.assertIn("display", cats_res.lower())
        print(f"  - search_components (action='categories'): {cats_res[:100]}...")

        # 2. Справочник режимов пинов
        pins_res = await search_components(action="pin_modes")
        self.assertIn("Справочник режимов пинов", pins_res)
        self.assertTrue("pcf8574" in pins_res.lower() or "mcp23xxx" in pins_res.lower() or "input" in pins_res.lower())
        print(f"  - search_components (action='pin_modes'): {pins_res[:100]}...")

    async def test_manage_secrets_list_and_privacy(self):
        """Тестирование чтения списка секретов с обеспечением приватности значений."""
        print("\n🔒 Запуск теста manage_secrets (list & privacy)...")
        res_list = await manage_secrets(action="list")
        self.assertIn("Зарегистрированные ключи секретов ESPHome", res_list)
        self.assertIn("приватные значения секретов не отображаются", res_list)
        print(f"  - manage_secrets (action='list'): успешно получены ключи без раскрытия значений")

    async def test_manage_secrets_lifecycle_with_snapshot_guard(self):
        """
        Тестирование записи секрета с гарантированным Teardown Guard.
        Сохраняет исходный снимок secrets.yaml и восстанавливает его байт-в-байт после завершения.
        """
        print("\n🛡 Запуск теста жизненного цикла manage_secrets с Teardown Guard...")

        secrets_path_candidates = [
            "/Users/andreyzolotnitskiy/Documents/github/esphome/config/secrets.yaml",
            os.path.expanduser("~/Documents/github/esphome/config/secrets.yaml"),
        ]
        target_secrets_path = None
        original_snapshot = None

        for cand in secrets_path_candidates:
            if os.path.exists(cand):
                target_secrets_path = cand
                with open(cand, "r", encoding="utf-8") as f:
                    original_snapshot = f.read()
                break

        uid = uuid.uuid4().hex[:6]
        temp_key = f"mcp_test_key_{uid}"
        temp_value = f"probe_val_{uid}"

        try:
            # 1. Запись временного секрета
            set_res = await manage_secrets(action="set", key=temp_key, value=temp_value)
            self.assertIn(f"Секрет `{temp_key}` успешно", set_res)
            self.assertIn("значение скрыто в целях безопасности", set_res)
            self.assertNotIn(temp_value, set_res)
            print(f"  - manage_secrets (action='set', key='{temp_key}'): успешно записан")

            # 2. Проверка появления ключа в списке
            list_res = await manage_secrets(action="list")
            self.assertIn(f"`{temp_key}`", list_res)
            print(f"  - manage_secrets (action='list'): временный ключ обнаружен в каталоге")

        finally:
            # 3. Teardown Guard: гарантированное восстановление secrets.yaml
            if target_secrets_path and original_snapshot is not None:
                print(f"  - Teardown Guard: восстановление `{target_secrets_path}` до исходного снимка...")
                with open(target_secrets_path, "w", encoding="utf-8") as f:
                    f.write(original_snapshot)
                print("  ✅ secrets.yaml успешно восстановлен в исходное состояние.")

            # Проверяем, что временный ключ исчез
            final_list = await manage_secrets(action="list")
            self.assertNotIn(f"`{temp_key}`", final_list)

    async def test_get_host_info_version_and_ports(self):
        """Тестирование получения версий сервера и Serial-портов."""
        print("\n🖥 Запуск тестов get_host_info (version & serial_ports)...")

        # 1. Version
        ver_res = await get_host_info(action="version")
        self.assertIn("Информация о сервере ESPHome", ver_res)
        self.assertIn("ESPHome Core Version", ver_res)
        self.assertIn("Device Builder Backend", ver_res)
        print(f"  - get_host_info (action='version'): {ver_res[:100]}...")

        # 2. Serial ports
        ports_res = await get_host_info(action="serial_ports")
        self.assertIn("Подключенные USB-Serial порты хоста", ports_res)
        print(f"  - get_host_info (action='serial_ports'): {ports_res[:100]}...")

        # 3. All
        all_res = await get_host_info(action="all")
        self.assertIn("Информация о сервере ESPHome", all_res)
        self.assertIn("Подключенные USB-Serial порты хоста", all_res)
        print(f"  - get_host_info (action='all'): успешно получена сводная информация")

    async def test_manage_labels_lifecycle(self):
        """Тестирование полного CRUD жизненного цикла каталога меток (manage_labels)."""
        print("\n🏷 Запуск тестов жизненного цикла manage_labels...")

        uid = uuid.uuid4().hex[:6]
        tag_name = f"TestTag_{uid}"
        upd_name = f"UpdTag_{uid}"
        tag_color = "#112233"
        upd_color = "#445566"
        created_lid = None

        try:
            # 1. Создание метки
            create_res = await manage_labels(action="create", name=tag_name, color=tag_color)
            self.assertIn(f"Метка `{tag_name}`", create_res)
            self.assertIn("успешно создана", create_res)
            print(f"  - manage_labels (create): {create_res}")

            # 2. Получение списка меток и поиск созданной
            list_res = await manage_labels(action="list")
            self.assertIn(tag_name, list_res)
            print("  - manage_labels (list): метка найдена в каталоге")

            # Извлекаем ID метки из текста ответа
            match = re.search(rf"- \*\*{tag_name}\*\*.*?ID: `([a-f0-9]+)`", list_res)
            self.assertIsNotNone(match, "ID созданной метки не найден в выводе manage_labels action='list'")
            created_lid = match.group(1)

            # 3. Обновление метки (имя и цвет)
            upd_res = await manage_labels(action="update", label_id=created_lid, name=upd_name, color=upd_color)
            self.assertIn("успешно обновлена", upd_res)
            self.assertIn(upd_name, upd_res)
            print(f"  - manage_labels (update): {upd_res}")

            # 4. Проверка обновленного списка
            list_upd = await manage_labels(action="list")
            self.assertIn(upd_name, list_upd)
            self.assertNotIn(tag_name, list_upd)

            # 5. Удаление метки
            del_res = await manage_labels(action="delete", label_id=created_lid)
            self.assertIn("успешно удалена", del_res)
            print(f"  - manage_labels (delete): {del_res}")

            # 6. Проверка отсутствия в каталоге
            final_list = await manage_labels(action="list")
            self.assertNotIn(upd_name, final_list)

        finally:
            # Teardown Guard: если созданная метка не была удалена
            if created_lid:
                await manage_labels(action="delete", label_id=created_lid)

    async def test_batch_manage_devices_lifecycle(self):
        """Тестирование пакетных операций над устройствами (batch_manage_devices)."""
        print("\n📦 Запуск тестов пакетных операций batch_manage_devices...")

        uid = uuid.uuid4().hex[:6]
        cfg1 = f"mcp-bulk-1-{uid}.yaml"
        cfg2 = f"mcp-bulk-2-{uid}.yaml"
        tag_name = f"BulkTag_{uid}"
        created_lid = None

        # 1. Создаем временную метку
        lbl_res = await manage_labels(action="create", name=tag_name, color="#00aa00")
        match = re.search(r"ID: `([a-f0-9]+)`", lbl_res)
        if match:
            created_lid = match.group(1)

        # 2. Создаем две временные фикстуры устройств
        yaml_content = f"esphome:\n  name: mcp-bulk-{uid}\nesp32:\n  board: esp32dev\n"
        await manage_device_config(action="create", configuration=cfg1, content=yaml_content)
        await manage_device_config(action="create", configuration=cfg2, content=yaml_content)

        try:
            # 3. Пакетное назначение меток (set_labels)
            if created_lid:
                bulk_lbl_res = await batch_manage_devices(
                    action="set_labels",
                    configurations=[cfg1, cfg2],
                    label_ids=[created_lid]
                )
                self.assertIn("Результат пакетной операции `set_labels`", bulk_lbl_res)
                self.assertIn(f"`{cfg1}`: ✅ Успешно", bulk_lbl_res)
                self.assertIn(f"`{cfg2}`: ✅ Успешно", bulk_lbl_res)
                print(f"  - batch_manage_devices (set_labels): метки успешно назначены на {cfg1} и {cfg2}")

            # 4. Пакетная архивация (archive)
            bulk_arc_res = await batch_manage_devices(
                action="archive",
                configurations=[cfg1, cfg2]
            )
            self.assertIn("Результат пакетной операции `archive`", bulk_arc_res)
            self.assertIn(f"`{cfg1}`: ✅ Успешно", bulk_arc_res)
            self.assertIn(f"`{cfg2}`: ✅ Успешно", bulk_arc_res)
            print(f"  - batch_manage_devices (archive): устройства успешно перемещены в архив")

        finally:
            # 5. Teardown Guard: очистка архивных фикстур и удаление временной метки
            from server import archive_devices
            await archive_devices(action="purge", configuration=cfg1)
            await archive_devices(action="purge", configuration=cfg2)
            if created_lid:
                await manage_labels(action="delete", label_id=created_lid)
            print("  ✅ Teardown пакетных фикстур завершен.")

    async def test_troubleshoot_device_states(self):
        """Тестирование получения таблицы онлайн/офлайн статусов устройств (action='states')."""
        print("\n📊 Запуск теста troubleshoot_device (action='states')...")
        res_states = await troubleshoot_device(action="states")
        self.assertIn("Статусы сетевой доступности устройств ESPHome", res_states)
        self.assertIn("Онлайн", res_states)
        self.assertIn("Офлайн", res_states)
        print(f"  - troubleshoot_device (states): {res_states[:120]}...")

    async def test_troubleshoot_device_probe(self):
        """Тестирование глубокой сетевой диагностики онлайн и офлайн устройств (action='probe')."""
        print("\n🔍 Запуск тестов troubleshoot_device (action='probe')...")

        # 1. Диагностика базового тестового устройства test.yaml
        res_probe_online = await troubleshoot_device(action="probe", configuration="test.yaml")
        self.assertIn("Сетевая диагностика устройства `test.yaml`", res_probe_online)
        self.assertIn("DNS разрешение:", res_probe_online)
        self.assertIn("mDNS / Zeroconf:", res_probe_online)
        self.assertIn("ICMP Ping:", res_probe_online)
        print(f"  - troubleshoot_device (probe test.yaml): успешно получены сетевые метрики")

        # 2. Диагностика устройства ina226.yaml (проверка локализации)
        res_probe_off = await troubleshoot_device(action="probe", configuration="ina226.yaml")
        self.assertIn("Сетевая диагностика устройства `ina226.yaml`", res_probe_off)
        self.assertIn("DNS разрешение:", res_probe_off)
        self.assertIn("ICMP Ping:", res_probe_off)
        print(f"  - troubleshoot_device (probe ina226.yaml): успешно диагностировано состояние")

    async def test_manage_version_history_log_show_diff(self):
        """Тестирование чтения истории коммитов, ревизий и diff (action='log', 'show', 'diff', 'deleted')."""
        print("\n📜 Запуск тестов manage_version_history (log, show, diff, deleted)...")

        # 1. log test.yaml
        res_log = await manage_version_history(action="log", configuration="test.yaml")
        self.assertIn("История версий конфигурации `test.yaml`", res_log)
        print("  - manage_version_history (log test.yaml): история успешно получена")

        # Извлекаем sha первого коммита
        match = re.search(r"- `([0-9a-fA-F]+)` \|", res_log)
        self.assertIsNotNone(match)
        first_sha = match.group(1)

        # 2. show test.yaml at sha
        res_show = await manage_version_history(action="show", configuration="test.yaml", sha=first_sha)
        self.assertIn(f"Содержимое `test.yaml` на момент коммита `{first_sha}`", res_show)
        self.assertIn("esphome:", res_show)
        print(f"  - manage_version_history (show {first_sha}): ревизия прочитана")

        # 3. diff test.yaml
        res_diff = await manage_version_history(action="diff", configuration="test.yaml")
        self.assertTrue("Различия (diff)" in res_diff or "изменения отсутствуют" in res_diff)
        print("  - manage_version_history (diff): успешно получен отчет сравнения")

        # 4. deleted configs
        res_del = await manage_version_history(action="deleted")
        self.assertTrue("Удаленные конфигурации в истории Git" in res_del or "не найдено записей" in res_del)
        print("  - manage_version_history (deleted): успешно проверена история удалений")

    async def test_manage_version_history_restore_lifecycle(self):
        """Тестирование полного жизненного цикла отката версии (restore) с Teardown Guard."""
        print("\n🔄 Запуск теста жизненного цикла manage_version_history (restore)...")
        uid = uuid.uuid4().hex[:6]
        cfg = f"mcp-vh-{uid}.yaml"

        content_v1 = f"esphome:\n  name: mcp-vh-{uid}\n  comment: v1_initial_snapshot\nesp32:\n  board: esp32dev\n"
        content_v2 = f"esphome:\n  name: mcp-vh-{uid}\n  comment: v2_modified_snapshot\nesp32:\n  board: esp32dev\n"

        # 1. Создаем v1
        create_res = await manage_device_config(action="create", configuration=cfg, content=content_v1)
        self.assertIn("создана", create_res.lower())

        try:
            # 2. Получаем sha коммита v1
            res_log1 = await manage_version_history(action="log", configuration=cfg)
            match1 = re.search(r"- `([0-9a-fA-F]+)` \|", res_log1)
            self.assertIsNotNone(match1)
            sha_v1 = match1.group(1)
            print(f"  - Создана фикстура v1 ({cfg}), SHA: {sha_v1}")

            # 3. Обновляем до v2
            upd_res = await manage_device_config(action="update", configuration=cfg, content=content_v2)
            self.assertIn("обновлена", upd_res.lower())

            # 4. Проверяем, что в текущей версии записано v2
            get_v2 = await manage_device_config(action="get", configuration=cfg)
            self.assertIn("v2_modified_snapshot", get_v2)

            # 5. Выполняем restore до v1 (sha_v1)
            res_restore = await manage_version_history(action="restore", configuration=cfg, sha=sha_v1)
            self.assertIn("успешно восстановлен", res_restore.lower())
            print(f"  - Выполнен restore {cfg} до {sha_v1}")

            # 6. Проверяем, что содержимое снова v1
            get_restored = await manage_device_config(action="get", configuration=cfg)
            self.assertIn("v1_initial_snapshot", get_restored)
            print(f"  - Подтверждено: содержимое {cfg} вернулось к v1_initial_snapshot")

        finally:
            # 7. Teardown Guard: удаление временного файла
            await manage_device_config(action="delete", configuration=cfg)
            print(f"  ✅ Teardown: временный файл {cfg} удален.")

    async def test_manage_automations_catalogs(self):
        """Тестирование каталогов триггеров, действий и условий (triggers, actions, conditions)."""
        print("\n⚡ Запуск тестов manage_automations (каталоги triggers, actions, conditions)...")

        # 1. triggers
        res_trig = await manage_automations(action="triggers", query="button")
        self.assertIn("Каталог триггеров автоматизаций ESPHome", res_trig)
        self.assertIn("button.", res_trig)
        print("  - manage_automations (triggers): каталог успешно получен")

        # 2. actions
        res_act = await manage_automations(action="actions", query="logger")
        self.assertIn("Каталог действий (Actions) ESPHome", res_act)
        self.assertIn("logger.", res_act)
        print("  - manage_automations (actions): каталог успешно получен")

        # 3. conditions
        res_cond = await manage_automations(action="conditions", query="switch")
        self.assertIn("Каталог условий (Conditions) ESPHome", res_cond)
        self.assertIn("switch.", res_cond)
        print("  - manage_automations (conditions): каталог успешно получен")

    async def test_manage_automations_parse_available(self):
        """Тестирование разбора AST и доступных сущностей (available, parse)."""
        print("\n📋 Запуск тестов manage_automations (available, parse)...")

        # 1. available test.yaml
        res_avail = await manage_automations(action="available", configuration="test.yaml")
        self.assertIn("Доступные сущности для автоматизаций в `test.yaml`", res_avail)
        self.assertIn("test_zabbix_alert", res_avail)
        print("  - manage_automations (available test.yaml): сущности получены")

        # 2. parse test.yaml
        res_parse = await manage_automations(action="parse", configuration="test.yaml")
        self.assertIn("Автоматизации устройства `test.yaml`", res_parse)
        self.assertIn("on_press", res_parse)
        print("  - manage_automations (parse test.yaml): AST дерево успешно разобрано")

    async def test_manage_automations_mutation_lifecycle(self):
        """Тестирование жизненного цикла точечной модификации AST автоматизаций (upsert/delete) с Teardown Guard."""
        print("\n🔧 Запуск теста жизненного цикла manage_automations (upsert, dry-run, apply, delete)...")
        uid = uuid.uuid4().hex[:6]
        cfg = f"mcp-auto-{uid}.yaml"
        init_yaml = f"esphome:\n  name: mcp-auto-{uid}\nesp32:\n  board: esp32dev\n\nswitch:\n  - platform: gpio\n    pin: GPIO2\n    id: test_switch\n    name: 'Test Switch'\n"

        # 1. Создаем устройство
        await manage_device_config(action="create", configuration=cfg, content=init_yaml)

        try:
            # 2. Dry-Run Upsert
            automation_payload = {
                "trigger_id": "switch.on_turn_on",
                "trigger_params": {},
                "actions": [
                    {
                        "action_id": "logger.log",
                        "params": {"format": "Switch Turned ON!"},
                        "conditions": []
                    }
                ]
            }
            res_dry = await manage_automations(
                action="upsert",
                configuration=cfg,
                component_id="test_switch",
                trigger="on_turn_on",
                automation=automation_payload,
                apply=False
            )
            self.assertIn("Предпросмотр вставки автоматизации", res_dry)
            self.assertIn("on_turn_on:", res_dry)
            print("  - manage_automations (upsert dry-run): diff рассчитан успешно")

            # 3. Apply Upsert
            res_apply = await manage_automations(
                action="upsert",
                configuration=cfg,
                component_id="test_switch",
                trigger="on_turn_on",
                automation=automation_payload,
                apply=True
            )
            self.assertIn("Автоматизация успешно добавлена", res_apply)
            print("  - manage_automations (upsert apply=True): автоматизация вставлена в YAML")

            # 4. Parse (проверяем, что AST распознает новую автоматизацию)
            res_parsed = await manage_automations(action="parse", configuration=cfg)
            self.assertIn("найдено: 1", res_parsed)
            self.assertIn("on_turn_on", res_parsed)
            print("  - manage_automations (parse): автоматизация успешно валидирована в AST")

            # 5. Delete (apply=True)
            res_delete = await manage_automations(
                action="delete",
                configuration=cfg,
                component_id="test_switch",
                trigger="on_turn_on",
                apply=True
            )
            self.assertIn("успешно удалена", res_delete)
            print("  - manage_automations (delete apply=True): автоматизация удалена из YAML")

            # 6. Parse после удаления
            res_after_del = await manage_automations(action="parse", configuration=cfg)
            self.assertIn("не обнаружено объявленных блоков", res_after_del)
            print("  - manage_automations (parse after delete): подтверждено отсутствие автоматизаций")

        finally:
            # 7. Teardown Guard
            await manage_device_config(action="delete", configuration=cfg)
            print(f"  ✅ Teardown: временный файл {cfg} удален.")

    async def test_get_firmware_binaries_list_and_token(self):
        """Тестирование получения списка бинарных артефактов и выпуска токенов (list, token)."""
        print("\n📦 Запуск тестов get_firmware_binaries (list, token)...")

        # 1. list для test.yaml
        res_list = await get_firmware_binaries(configuration="test.yaml", action="list")
        self.assertIn("Скомпилированные артефакты прошивки для `test.yaml`", res_list)
        self.assertIn("firmware.factory.bin", res_list)
        print("  - get_firmware_binaries (list test.yaml): список артефактов успешно получен")

        # 2. token для test.yaml (firmware.factory.bin)
        res_tok = await get_firmware_binaries(
            configuration="test.yaml",
            action="token",
            file="firmware.factory.bin"
        )
        self.assertIn("Токен и ссылка для скачивания `test-firmware.factory.bin`", res_tok)
        self.assertIn("/api/firmware/download?token=", res_tok)
        print("  - get_firmware_binaries (token test.yaml): токен и ссылка сгенерированы успешно")

    async def test_get_firmware_binaries_download_lifecycle(self):
        """Тестирование скачивания бинарного артефакта на диск (download) с Teardown Guard."""
        print("\n📥 Запуск теста жизненного цикла get_firmware_binaries (download)...")
        tmp_download_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "scratch", f"test_fw_{uuid.uuid4().hex[:6]}.factory.bin")
        )

        try:
            # 1. Скачиваем бинарник
            res_down = await get_firmware_binaries(
                configuration="test.yaml",
                action="download",
                file="firmware.factory.bin",
                save_path=tmp_download_path
            )
            self.assertIn("Артефакт прошивки успешно скачан", res_down)
            self.assertIn("SHA-256", res_down)
            print("  - get_firmware_binaries (download test.yaml): бинарник успешно скачан через HTTP")

            # 2. Проверяем файл на диске
            self.assertTrue(os.path.exists(tmp_download_path))
            file_size = os.path.getsize(tmp_download_path)
            self.assertGreater(file_size, 100 * 1024)  # > 100 KB
            print(f"  - Проверка файла на диске: размер {file_size:,} байт — подтвержден")

        finally:
            # 3. Teardown Guard: удаление скачанного тестового файла
            if os.path.exists(tmp_download_path):
                os.remove(tmp_download_path)
                print(f"  ✅ Teardown: временный бинарный файл {tmp_download_path} удален.")


if __name__ == "__main__":
    unittest.main()
