from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)




class RegistryAssetsMixin:
    @staticmethod
    def _username() -> str:
        username = str(request.username or "").strip()
        if not username:
            raise PermissionError("需要登录 AstrBot 管理后台")
        return username
    @classmethod
    def _actor(cls) -> str:
        return f"web:{cls._username()}"
    def _web_principal(self) -> dict[str, Any]:
        """解析当前 Web 请求主体与能力声明。

        该主体用于“玩家域 / 副本域”接口（session_dm /
        session_member），必须携带真实平台绑定。只有插件 security.admin_ids
        显式包含该用户名、或宿主 context 明确声明其为管理员时，
        is_admin 才为 True；无法映射角色时默认拒绝高权限写操作。
        """
        username = self._username()
        config = self._config()
        request_auth_source = str(
            getattr(request, "auth_source", "") or ""
        ).strip()
        is_remote_panel = request_auth_source == "remote_panel"
        is_admin = is_remote_panel or config.is_admin(username)
        role_source = (
            "remote_panel"
            if is_remote_panel
            else "config_admin_ids"
            if is_admin
            else "unmapped"
        )
        context = self.context
        if context is not None:
            try:
                declared = getattr(context, "is_admin", None)
                if callable(declared):
                    if bool(declared(username)):
                        is_admin = True
                        role_source = "host_context"
                else:
                    for attr in ("admin_usernames", "admin_users"):
                        names = getattr(context, attr, None)
                        if names is None:
                            continue
                        if username in {str(name) for name in names}:
                            is_admin = True
                            role_source = "host_context"
                            break
            except Exception:
                pass
        return {
            "username": username,
            "auth_source": (
                "remote_panel" if is_remote_panel else "platform_binding"
            ),
            "is_admin": is_admin,
            "role_source": role_source,
            "capabilities": {
                "admin": is_admin,
                "author": is_admin,
                "world_install": is_admin,
                "economy": is_admin,
                "dm": is_admin,
            },
        }
    def _console_principal(self) -> dict[str, Any]:
        """AstrBot 原生管理页主体；不与消息平台用户 ID 混用。

        该主体只代表控制台登录身份，绝不自动携带 QQ/OpenID。
        作者任务、健康中心、作者实验室、世界包管理等纯控制台入口使用
        该主体授权；玩家域接口不得使用该主体冒充平台成员。
        """

        username = self._username()
        request_auth_source = str(
            getattr(request, "auth_source", "") or ""
        ).strip()
        is_remote_panel = request_auth_source == "remote_panel"
        return {
            "username": username,
            "auth_source": (
                "remote_panel" if is_remote_panel else "astrbot_console"
            ),
            "is_admin": True,
            "role_source": (
                "remote_panel" if is_remote_panel else "astrbot_console"
            ),
            "capabilities": {
                "admin": True,
                "author": True,
                "world_install": True,
                "economy": True,
                "dm": True,
            },
        }
    def _require_console_admin(self) -> dict[str, Any]:
        """要求 AstrBot 管理后台身份，无需额外插件登录。"""

        return self._console_principal()
    def _require_admin(self) -> dict[str, Any]:
        """高权限写操作（世界重装、作者实验室编辑、面板凭据重置）的门禁。

        这些入口只在原生 AstrBot 管理页出现，按控制台身份授权，
        不再把后台用户名误查 security.admin_ids。
        """
        principal = self._console_principal()
        if not principal["is_admin"]:
            raise PolicyRejection(
                "需要插件管理员权限；当前登录用户未在插件 security.admin_ids"
                "或宿主管理员声明中"
            )
        return principal
    def _require_author(self) -> dict[str, Any]:
        """作者实验室写入权限（控制台身份，与管理员一致）。"""
        return self._require_admin()
    async def _builtin_statuses(self) -> list[dict[str, Any]]:
        """读取内置世界安装状态；没有宿主注入时安全返回空列表。"""
        provider = self.builtin_world_status_provider
        if provider is None:
            return []
        try:
            value = provider()
            if inspect.isawaitable(value):
                value = await value
        except Exception:
            return []
        if isinstance(value, list):
            return [
                dict(item)
                for item in value
                if isinstance(item, Mapping)
            ]
        return []
    async def _ensure_builtin_worlds(self) -> None:
        """Run the host-provided idempotent recovery before listing worlds."""

        callback = self.builtin_world_ensure
        if callback is None:
            return
        try:
            value = callback()
            if inspect.isawaitable(value):
                await value
        except Exception:
            self.logger.exception("内置世界自动恢复失败，世界库将显示可重试状态")
    async def dashboard_sessions(self):
        """副本概览列表：状态 / 世界 / 当前行动者 / 活跃计时器数。"""
        try:
            self._username()
            sessions = await build_dashboard_sessions(self.database)
            return json_response({"sessions": sessions})
        except Exception as exc:
            return self._handle_error(exc)
