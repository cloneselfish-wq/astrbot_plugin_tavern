from __future__ import annotations

from .plugin_shared import *
from .startup import StartupMethods
from .delivery import DeliveryMethods
from .messages import MessageMethods
from .commands import CommandMethods
from .legacy_commands import LegacyCommandMethods
from .background_jobs import BackgroundJobMethods
from .webhooks import WebhookMethods
from .shutdown import ShutdownMethods

# Dynamic help title contract: 【321开团 v{PLUGIN_VERSION}｜



class PluginLifecycleMixin:
    async def terminate(self):
        bootstrap = self._builtin_world_bootstrap_task
        if bootstrap is not None and not bootstrap.done():
            bootstrap.cancel()
            await asyncio.gather(bootstrap, return_exceptions=True)
        self._builtin_world_bootstrap_task = None
        await self.delivery_worker.stop()
        panel_server = self._panel_server
        panel_thread = self._panel_thread
        if panel_server is not None:
            try:
                panel_server.shutdown()
            except Exception:
                logger.exception("321开团独立面板关闭失败")
            finally:
                try:
                    panel_server.server_close()
                except Exception:
                    logger.exception("321开团独立面板端口释放失败")
        if panel_thread is not None:
            try:
                await asyncio.to_thread(panel_thread.join, 5.0)
                if panel_thread.is_alive():
                    logger.warning("321开团独立面板线程在关闭超时后仍未退出")
            except Exception:
                logger.exception("321开团独立面板线程等待失败")
        self._panel_server = None
        self._panel_thread = None
        self._panel_runtime_status = {
            "state": "stopped",
            "message": "独立面板已经停止。",
            "recovery": "重新加载插件后会按当前配置启动。",
        }
        await self._background.close()
        self.database.defer_storage_sync = False
        self._timer_task = None
        self._backup_task = None
        self._webhook_task = None
        self._event_outbox_task = None
        self._storage_sync_task = None
        self._author_job_task = None
        await self.broker.close()
        logger.info("321开团已停止。")
