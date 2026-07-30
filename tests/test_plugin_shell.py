from __future__ import annotations

import hashlib
import importlib
import io
import re
import shutil
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs(data_dir: Path) -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    web = types.ModuleType("astrbot.api.web")

    class Logger:
        def debug(self, *args, **kwargs):
            return None

        info = debug
        warning = debug
        exception = debug

    class AstrBotConfig(dict):
        def save_config(self):
            self.save_calls = getattr(self, "save_calls", 0) + 1
            return None

    class AstrMessageEvent:
        pass

    class Filter:
        class EventMessageType:
            GROUP_MESSAGE = "group"

        class CommandGroup:
            def __init__(self, name, alias=None, **kwargs):
                self.name = name
                self.alias = alias or set()
                self.priority = kwargs.get("priority", 0)
                self.commands = {}

            def command(self, name, alias=None, **kwargs):
                def decorator(function):
                    self.commands[name] = {
                        "alias": alias or set(),
                        "priority": kwargs.get("priority", 0),
                        "handler": function,
                    }
                    return function

                return decorator

        @staticmethod
        def command_group(name, alias=None, **kwargs):
            def decorator(_function):
                return Filter.CommandGroup(name, alias, **kwargs)

            return decorator

        @staticmethod
        def event_message_type(*args, **kwargs):
            def decorator(function):
                function.__astrbot_event_priority__ = kwargs.get(
                    "priority",
                    0,
                )
                return function

            return decorator

        @staticmethod
        def on_astrbot_loaded(*args, **kwargs):
            return lambda function: function

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(_name):
            return data_dir

    class PluginUploadFile:
        pass

    def response(value=None, *args, **kwargs):
        return value

    api.AstrBotConfig = AstrBotConfig
    api.logger = Logger()
    event.AstrMessageEvent = AstrMessageEvent
    event.filter = Filter()
    star.Context = Context
    star.Star = Star
    star.StarTools = StarTools
    web.PluginUploadFile = PluginUploadFile
    web.error_response = response
    web.file_response = response
    web.json_response = response
    web.stream_response = response
    web.request = SimpleNamespace(username=None)

    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api.web"] = web


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[tuple] = []

    def register_web_api(self, *args):
        self.routes.append(args)

    def get_all_providers(self):
        return [
            SimpleNamespace(
                meta=lambda: SimpleNamespace(
                    id="story-main",
                    name="主叙事模型",
                    model="narrative-pro",
                )
            ),
            SimpleNamespace(
                meta=lambda: {
                    "id": "vision-model",
                    "provider_name": "图片模型",
                    "model_name": "vision-pro",
                }
            ),
        ]


class PluginShellTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        _install_astrbot_stubs(Path(self.temp_dir.name))
        sys.path.insert(0, str(ROOT.parent))
        for name in list(sys.modules):
            if name == "astrbot_plugin_tavern" or name.startswith(
                "astrbot_plugin_tavern."
            ):
                sys.modules.pop(name)
        module = importlib.import_module("astrbot_plugin_tavern.main")
        self.module = module
        self.context = FakeContext()
        self.config = sys.modules["astrbot.api"].AstrBotConfig(
            {
                "security": {
                    "admin_ids": ["admin-1"],
                    "allowed_group_ids": ["group-shell"],
                    "require_group_whitelist": True,
                    "public_status": True,
                }
            }
        )
        self.plugin = module.TavernPlugin(self.context, self.config)

    async def asyncTearDown(self) -> None:
        await self.plugin.terminate()
        sys.modules["astrbot.api.web"].request.username = None
        if str(ROOT.parent) in sys.path:
            sys.path.remove(str(ROOT.parent))
        self.temp_dir.cleanup()

    async def test_plugin_registers_native_web_routes(self) -> None:
        paths = {route[0] for route in self.context.routes}
        self.assertGreaterEqual(len(paths), 20)
        self.assertIn("/astrbot_plugin_tavern/overview", paths)
        self.assertIn("/astrbot_plugin_tavern/worlds/restore", paths)
        self.assertIn("/astrbot_plugin_tavern/sessions/turn-order", paths)
        self.assertIn("/astrbot_plugin_tavern/settings/save", paths)
        self.assertIn("/astrbot_plugin_tavern/providers", paths)
        self.assertIn("/astrbot_plugin_tavern/groups/remark", paths)
        self.assertIn("/astrbot_plugin_tavern/backup/import/<mode>", paths)
        self.assertIn("/astrbot_plugin_tavern/events", paths)

    async def test_full_backup_zip_requires_safe_verified_members(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.web_console import (
            _verify_backup_archive,
        )

        payload = b'{"format":"astrbot-tavern-backup"}'
        checksum = hashlib.sha256(payload).hexdigest()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("bundle.json", payload)
            archive.writestr(
                "checksum.sha256",
                f"{checksum}  bundle.json\n",
            )
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            self.assertEqual(
                _verify_backup_archive(archive),
                {"bundle.json": checksum},
            )

        unsafe = io.BytesIO()
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../bundle.json", payload)
            archive.writestr(
                "checksum.sha256",
                f"{checksum}  ../bundle.json\n",
            )
        unsafe.seek(0)
        with zipfile.ZipFile(unsafe) as archive:
            with self.assertRaises(ValueError):
                _verify_backup_archive(archive)

    async def test_full_backup_zip_restores_independent_save_files(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import DEFAULT_WORLD_SLUG

        request = sys.modules["astrbot.api.web"].request
        request.username = "dashboard-admin"
        session = await self.plugin.database.ensure_session(
            "qq",
            "group-backup",
            "qq:group-backup",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.create_snapshot(
            session["id"],
            "导出前手动存档",
            "admin-1",
        )
        storage = await self.plugin.database.get_storage_info(session["id"])
        story_dir = Path(self.temp_dir.name) / storage["relative_path"]
        original_save = next((story_dir / "saves").glob("save_*.zip"))

        exported = await self.plugin.web_console.backup_export()
        self.assertTrue(Path(exported).exists())
        with zipfile.ZipFile(exported) as archive:
            self.assertIn("bundle.json", archive.namelist())
            self.assertIn("catalog.sqlite3", archive.namelist())
            self.assertTrue(
                any(
                    name.endswith(f"/saves/{original_save.name}")
                    for name in archive.namelist()
                )
            )

        original_save.unlink()
        self.assertFalse(original_save.exists())
        upload_type = sys.modules[
            "astrbot.api.web"
        ].PluginUploadFile

        class Upload(upload_type):
            filename = Path(exported).name

            async def save(self, destination):
                shutil.copyfile(exported, destination)

        async def uploaded_files():
            return {"file": Upload()}

        request.files = uploaded_files
        result = await self.plugin.web_console.backup_import("replace")
        self.assertIn("imported", result)
        self.assertTrue(original_save.exists())

    async def test_web_console_lists_configured_model_providers(self) -> None:
        request = sys.modules["astrbot.api.web"].request
        request.username = "dashboard-admin"
        response = await self.plugin.web_console.providers()
        self.assertEqual(
            [item["id"] for item in response["items"]],
            ["story-main", "vision-model"],
        )
        self.assertEqual(response["items"][0]["name"], "主叙事模型")
        self.assertEqual(response["items"][1]["model"], "vision-pro")

    async def test_closed_session_reopens_current_world_without_reset(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        config = TavernConfig.from_mapping(self.config)
        event = SimpleNamespace(unified_msg_origin="qq:group-shell")
        response = await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(
                matched=True,
                action="start",
                argument="border-tavern",
            ),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        self.assertIn("酒馆已开启", response)
        session = await self.plugin.database.get_session_by_group(
            "qq",
            "group-shell",
        )
        original_world = session["world_id"]

        await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(matched=True, action="close"),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(
                matched=True,
                action="start",
                argument="border-tavern",
            ),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        reopened = await self.plugin.database.get_session_by_group(
            "qq",
            "group-shell",
        )
        self.assertEqual(reopened["world_id"], original_world)

    def test_group_id_prefers_unified_event_accessor(self) -> None:
        event = SimpleNamespace(
            get_group_id=lambda: "canonical-group",
            message_obj=SimpleNamespace(group_id="adapter-field"),
        )
        self.assertEqual(
            self.plugin._group_id(event),
            "canonical-group",
        )

    async def test_unauthorized_command_is_audited_without_execution(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        config = TavernConfig.from_mapping(self.config)
        response = await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
            command=ParsedCommand(matched=True, action="start"),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="intruder",
        )
        self.assertIsNone(response)
        self.assertIsNone(
            await self.plugin.database.get_session_by_group(
                "qq",
                "group-shell",
            )
        )
        audit = await self.plugin.database.list_audit("", 20, 0)
        denied = [
            item
            for item in audit
            if item["action"] == "security.command_denied"
        ]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["actor_id"], "intruder")
        self.assertEqual(
            denied[0]["detail"]["reason"],
            "sender_not_authorized",
        )

    async def test_authorized_start_auto_binds_then_requires_selection(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        group_id = "group-auto-bound"
        response = await self.plugin._handle_command(
            event=SimpleNamespace(
                unified_msg_origin=f"qq-instance:{group_id}"
            ),
            command=ParsedCommand(matched=True, action="start"),
            config=TavernConfig.from_mapping(self.config),
            group_id=group_id,
            platform_id="qq-instance",
            sender_id="admin-1",
        )

        self.assertIn("本群还没有酒馆副本", response)
        self.assertNotIn("酒馆已开启", response)
        self.assertIn("平台实例 ID：qq-instance", response)
        self.assertIn(f"群 ID：{group_id}", response)
        self.assertIn(
            group_id,
            self.config["security"]["allowed_group_ids"],
        )
        self.assertEqual(self.config.save_calls, 1)
        self.assertIsNone(
            await self.plugin.database.get_session_by_group(
                "qq-instance",
                group_id,
            )
        )
        audit = await self.plugin.database.list_audit("", 20, 0)
        bindings = [
            item
            for item in audit
            if item["action"] == "security.group_auto_allowed"
        ]
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["actor_id"], "admin-1")
        self.assertEqual(
            bindings[0]["detail"]["source"],
            "authorized_group_command",
        )

        started = await self.plugin._handle_command(
            event=SimpleNamespace(
                unified_msg_origin=f"qq-instance:{group_id}"
            ),
            command=ParsedCommand(
                matched=True,
                action="start",
                argument="border-tavern",
            ),
            config=TavernConfig.from_mapping(self.config),
            group_id=group_id,
            platform_id="qq-instance",
            sender_id="admin-1",
        )
        self.assertIn("酒馆已开启", started)
        self.assertIn("副本标识：border-tavern", started)

    def test_instance_list_includes_intro_and_paginates_five_at_a_time(
        self,
    ) -> None:
        worlds = [
            {
                "slug": f"world-{index}",
                "name": f"测试世界 {index}",
                "description": f"这是第 {index} 个世界的简介。",
            }
            for index in range(1, 8)
        ]

        first_page = self.module.format_instance_list([], worlds, page=1)
        self.assertIn("第 1/2 页", first_page)
        self.assertEqual(first_page.count("简介："), 5)
        self.assertIn("这是第 1 个世界的简介", first_page)
        self.assertIn("这是第 5 个世界的简介", first_page)
        self.assertNotIn("这是第 6 个世界的简介", first_page)
        self.assertIn("/酒馆 开启 第2页", first_page)

        second_page = self.module.format_instance_list([], worlds, page=2)
        self.assertIn("第 2/2 页", second_page)
        self.assertEqual(second_page.count("简介："), 2)
        self.assertNotIn("这是第 5 个世界的简介", second_page)
        self.assertIn("这是第 6 个世界的简介", second_page)
        self.assertIn("这是第 7 个世界的简介", second_page)
        self.assertIn("/酒馆 开启 第1页", second_page)
        self.assertEqual(
            self.module.parse_instance_list_page("第 2 页"),
            2,
        )
        self.assertIsNone(
            self.module.parse_instance_list_page("border-tavern")
        )

    async def test_start_page_argument_only_reads_requested_page(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        for index in range(1, 7):
            await self.plugin.database.save_world(
                {
                    "slug": f"page-world-{index}",
                    "name": f"分页世界 {index}",
                    "description": f"分页简介 {index}",
                    "system_prompt": "保持因果一致。",
                    "opening_scene": "故事尚未开始。",
                    "rules": {"resolution": "d20"},
                    "initial_state": {
                        "location": "起点",
                        "facts": [],
                        "inventory": {},
                        "relationships": {},
                    },
                },
                "admin-1",
            )

        response = await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
            command=ParsedCommand(
                matched=True,
                action="start",
                argument="第2页",
            ),
            config=TavernConfig.from_mapping(self.config),
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )

        self.assertIn("第 2/2 页", response)
        self.assertEqual(response.count("简介："), 2)
        self.assertNotIn("酒馆已开启", response)
        self.assertIsNone(
            await self.plugin.database.get_session_by_group(
                "qq",
                "group-shell",
            )
        )

    async def test_start_without_argument_only_lists_existing_instances(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.constants import SESSION_RUNNING
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        first = await self.plugin.database.ensure_session(
            "qq",
            "group-shell",
            "qq:group-shell",
            "border-tavern",
            "admin-1",
            "main-copy",
            "主线副本",
        )
        first = await self.plugin.database.transition_session(
            first["id"],
            SESSION_RUNNING,
            "admin-1",
        )
        second = await self.plugin.database.ensure_session(
            "qq",
            "group-shell",
            "qq:group-shell",
            "border-tavern",
            "admin-1",
            "second-copy",
            "二周目副本",
        )
        before = {
            item["id"]: (
                item["state"],
                item["revision"],
                item["selected"],
            )
            for item in (first, second)
        }

        response = await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
            command=ParsedCommand(matched=True, action="start"),
            config=TavernConfig.from_mapping(self.config),
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )

        self.assertIn("请选择酒馆副本", response)
        self.assertIn("主线副本", response)
        self.assertIn("（main-copy）", response)
        self.assertIn("二周目副本", response)
        self.assertIn("（second-copy）", response)
        self.assertIn("简介：", response)
        self.assertIn("一座位于诸界夹缝中的中立酒馆", response)
        self.assertNotIn("酒馆已开启", response)
        after_items = await self.plugin.database.list_group_sessions(
            "qq",
            "group-shell",
        )
        after = {
            item["id"]: (
                item["state"],
                item["revision"],
                item["selected"],
            )
            for item in after_items
        }
        self.assertEqual(after, before)

    async def test_unlisted_group_cannot_be_bound_by_non_admin(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        group_id = "group-intruder"
        response = await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin=f"qq:{group_id}"),
            command=ParsedCommand(matched=True, action="start"),
            config=TavernConfig.from_mapping(self.config),
            group_id=group_id,
            platform_id="qq",
            sender_id="intruder",
        )

        self.assertIsNone(response)
        self.assertNotIn(
            group_id,
            self.config["security"]["allowed_group_ids"],
        )
        self.assertIsNone(
            await self.plugin.database.get_session_by_group("qq", group_id)
        )

    async def test_missing_admin_configuration_returns_setup_error(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        self.config["security"]["admin_ids"] = []
        response = await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin="qq:group-new"),
            command=ParsedCommand(matched=True, action="start"),
            config=TavernConfig.from_mapping(self.config),
            group_id="group-new",
            platform_id="qq",
            sender_id="first-user",
        )

        self.assertIn("酒馆尚未初始化", response)
        self.assertIn("管理员 ID", response)
        self.assertIsNone(
            await self.plugin.database.get_session_by_group(
                "qq",
                "group-new",
            )
        )

    def test_management_commands_use_native_command_group(self) -> None:
        group = self.module.TavernPlugin.tavern
        self.assertEqual(group.name, "酒馆")
        self.assertGreater(
            group.commands["开启"]["priority"],
            self.module.TavernPlugin.on_group_message.__astrbot_event_priority__,
        )
        self.assertEqual(
            set(group.commands),
            {
                "开启",
                "开演",
                "暂停",
                "继续",
                "关闭",
                "完结",
                "强制终止",
                "维护",
                "状态",
                "安全暂停",
                "存档",
                "读档",
                "回滚",
                "世界列表",
                "副本列表",
                "加入",
                "建卡",
                "填写",
                "预览",
                "重填数值",
                "确认建卡",
                "取消建卡",
                "角色",
                "准备",
                "阵容",
                "审核",
                "选择",
                "灵感",
                "灵感重投",
                "重整选项",
                "投票",
                "暂离",
                "返回队列",
                "申请返场",
                "退出",
                "顺序",
                "跳过",
                "下一位",
                "帮助",
            },
        )
        self.assertEqual(group.commands["开启"]["alias"], {"启动"})
        self.assertEqual(group.commands["继续"]["alias"], {"恢复"})
        self.assertEqual(group.commands["顺序"]["alias"], {"轮次"})
        self.assertEqual(group.commands["副本列表"]["alias"], {"副本"})

    async def test_chat_review_lists_views_and_approves_pending_card(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-shell",
            "qq:group-shell",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        session = await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        for index in range(2):
            user_id = f"review-user-{index + 1}"
            reserved = await self.plugin.database.reserve_participant(
                session["id"],
                user_id,
                f"待审玩家{index + 1}",
            )
            origin = f"qq:friend-{user_id}"
            bound = await self.plugin.database.bind_card_code(
                reserved["binding_code"],
                user_id,
                origin,
            )
            for field in bound["template"]["fields"]:
                if field["key"] == "name":
                    value = f"待审角色{index + 1}"
                elif field["key"] == "code":
                    value = f"R{index + 1}"
                elif field.get("type") == "integer":
                    value = str(field.get("default", 0))
                elif field.get("private"):
                    value = "审核用私密内容"
                else:
                    value = "审核用公开内容"
                await self.plugin.database.fill_card_draft(origin, value)
            await self.plugin.database.confirm_card_draft(origin)

        class NativeReviewEvent:
            message_str = "酒馆 审核"
            unified_msg_origin = "qq:group-shell"
            message_obj = SimpleNamespace(group_id="group-shell")

            def __init__(self) -> None:
                self.stopped = False

            @staticmethod
            def get_group_id():
                return "group-shell"

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_sender_id():
                return "admin-1"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        native_event = NativeReviewEvent()
        native_listed = [
            item async for item in self.plugin.tavern_review(native_event)
        ]
        self.assertTrue(native_event.stopped)
        self.assertIn("待审核角色卡", native_listed[0])

        event = SimpleNamespace(unified_msg_origin="qq:group-shell")
        config = TavernConfig.from_mapping(self.config)
        listed = await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(matched=True, action="review"),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        self.assertIn("待审核角色卡", listed)
        self.assertIn("待审角色1", listed)
        self.assertIn("待审角色2", listed)
        review_reference = re.search(
            r"审核号：(R-[A-Z0-9]{8})",
            listed,
        ).group(1)

        detail = await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(
                matched=True,
                action="review",
                argument="查看 1",
            ),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        self.assertIn("角色卡审核详情", detail)
        self.assertIn("审核用公开内容", detail)
        self.assertIn("已填写，群聊中隐藏", detail)
        self.assertNotIn("审核用私密内容", detail)
        self.assertIn("体魄", detail)

        approved = await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(
                matched=True,
                action="review",
                argument=f"{review_reference} 通过 群聊审核通过",
            ),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        self.assertIn("已通过", approved)
        self.assertIn("剩余待审核：1 人", approved)
        roster = await self.plugin.database.list_roster(session["id"])
        self.assertEqual(
            sum(item["card_status"] == "approved" for item in roster),
            1,
        )

    async def test_private_stat_prompt_and_native_reset_keep_profile(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-stat-prompt",
            "qq:group-stat-prompt",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        reserved = await self.plugin.database.reserve_participant(
            session["id"],
            "prompt-user",
            "提示玩家",
        )

        class Event:
            unified_msg_origin = "qq:friend-prompt-user"
            message_obj = SimpleNamespace(group_id="")

            def __init__(self, message: str) -> None:
                self.message_str = message
                self.stopped = False

            @staticmethod
            def get_group_id():
                return ""

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_sender_id():
                return "prompt-user"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event(f"酒馆 建卡 {reserved['binding_code']}")
        _ = [item async for item in self.plugin.tavern_card(event)]
        draft = await self.plugin.database.card_draft_for_private(
            event.unified_msg_origin
        )
        stat_fields = [
            item for item in draft["template"]["fields"]
            if item.get("stat_key")
        ]
        first_stat_step = draft["template"]["fields"].index(stat_fields[0])
        response = ""
        for field in draft["template"]["fields"][:first_stat_step]:
            if field["key"] == "name":
                value = "提示角色"
            elif field["key"] == "code":
                value = "TIP"
            else:
                value = "不会被数值重填删除"
            event.message_str = f"酒馆 填写 {value}"
            response = [
                item async for item in self.plugin.tavern_card_fill(event)
            ][0]
        self.assertIn("接下来开始填写角色数值", response)
        self.assertIn("总预算：10 点", response)

        for value in ("5", "4"):
            event.message_str = f"酒馆 填写 {value}"
            response = [
                item async for item in self.plugin.tavern_card_fill(event)
            ][0]
        self.assertIn("当前可填：0—1", response)

        event.message_str = "酒馆 重填数值"
        reset = [
            item
            async for item in self.plugin.tavern_card_stats_reset(event)
        ][0]
        self.assertIn("角色数值已重置", reset)
        self.assertIn("当前可填：0—5", reset)
        stored = await self.plugin.database.card_draft_for_private(
            event.unified_msg_origin
        )
        self.assertEqual(
            stored["fields"]["background"],
            "不会被数值重填删除",
        )
        self.assertFalse(
            any(key.startswith("stat_") for key in stored["fields"])
        )

    async def test_native_start_uses_stripped_text_and_real_event_ids(
        self,
    ) -> None:
        class Event:
            message_str = "酒馆 开启"
            unified_msg_origin = "qq-live:group-live"
            message_obj = SimpleNamespace(group_id="adapter-group")

            def __init__(self) -> None:
                self.stopped = False

            def get_group_id(self):
                return "group-live"

            def get_platform_id(self):
                return "qq-live"

            def get_sender_id(self):
                return "admin-1"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event()
        selection_responses = [
            item async for item in self.plugin.tavern_start(event)
        ]

        self.assertTrue(event.stopped)
        self.assertEqual(len(selection_responses), 1)
        self.assertIn(
            "平台实例 ID：qq-live",
            selection_responses[0],
        )
        self.assertIn("群 ID：group-live", selection_responses[0])
        self.assertNotIn("酒馆已开启", selection_responses[0])
        self.assertIn(
            "group-live",
            self.config["security"]["allowed_group_ids"],
        )
        self.assertIsNone(
            await self.plugin.database.get_session_by_group(
                "qq-live",
                "group-live",
            )
        )

        event.message_str = "酒馆 开启 border-tavern"
        started_responses = [
            item async for item in self.plugin.tavern_start(event)
        ]
        self.assertEqual(len(started_responses), 1)
        self.assertIn("酒馆已开启", started_responses[0])

    async def test_private_native_card_commands_accept_halfwidth_slash(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-private-command",
            "qq:group-private-command",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        reserved = await self.plugin.database.reserve_participant(
            session["id"],
            "private-user",
            "私聊玩家",
        )

        class Event:
            unified_msg_origin = "qq:friend-private-user"
            message_obj = SimpleNamespace(group_id="")

            def __init__(self, message: str) -> None:
                self.message_str = message
                self.stopped = False

            @staticmethod
            def get_group_id():
                return ""

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_sender_id():
                return "private-user"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event(f"酒馆 建卡 {reserved['binding_code']}")
        bound = [item async for item in self.plugin.tavern_card(event)]

        self.assertTrue(event.stopped)
        self.assertEqual(len(bound), 1)
        self.assertIn("私聊身份绑定成功", bound[0])
        self.assertIsNotNone(
            await self.plugin.database.card_draft_for_private(
                event.unified_msg_origin
            )
        )

        event.message_str = "酒馆 预览"
        halfwidth_preview = [
            item async for item in self.plugin.tavern_card_preview(event)
        ]
        self.assertIn("角色卡预览", halfwidth_preview[0])

        event.message_str = "／酒馆 预览"
        fullwidth_preview = [
            item async for item in self.plugin.on_private_message(event)
        ]
        self.assertIn("角色卡预览", fullwidth_preview[0])

    async def test_native_command_preserves_full_trailing_argument(
        self,
    ) -> None:
        class Event:
            message_str = "酒馆 开启 border-tavern"
            unified_msg_origin = "qq:group-shell"
            message_obj = SimpleNamespace(group_id="group-shell")

            def __init__(self) -> None:
                self.stopped = False

            def get_group_id(self):
                return "group-shell"

            def get_platform_id(self):
                return "qq"

            def get_sender_id(self):
                return "admin-1"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event()
        _ = [item async for item in self.plugin.tavern_start(event)]
        event.message_str = "酒馆 存档 旧塔 之前"
        responses = [item async for item in self.plugin.tavern_save(event)]

        self.assertTrue(event.stopped)
        self.assertEqual(len(responses), 1)
        self.assertIn("只有正式运行中的故事可以创建新剧情存档", responses[0])

    async def test_group_listener_intercepts_stripped_unknown_command(
        self,
    ) -> None:
        class Event:
            message_str = "酒馆 不存在"
            is_at_or_wake_command = True
            unified_msg_origin = "qq:group-shell"
            message_obj = SimpleNamespace(group_id="group-shell")

            def __init__(self) -> None:
                self.stopped = False

            def get_group_id(self):
                return "group-shell"

            def get_platform_id(self):
                return "qq"

            def get_sender_id(self):
                return "admin-1"

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event()
        responses = [
            item async for item in self.plugin.on_group_message(event)
        ]

        self.assertTrue(event.stopped)
        self.assertEqual(len(responses), 1)
        self.assertIn("未知命令：不存在", responses[0])

    async def test_group_listener_ignores_unprefixed_chat_and_strips_jg(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
            command=ParsedCommand(
                matched=True,
                action="start",
                argument="border-tavern",
            ),
            config=TavernConfig.from_mapping(self.config),
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )

        class Event:
            is_at_or_wake_command = False
            unified_msg_origin = "qq:group-shell"
            message_obj = SimpleNamespace(group_id="group-shell")

           
