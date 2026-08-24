from __future__ import annotations

from .plugin_shared import *


class ShutdownMethods:
    async def _backup_loop(self) -> None:
        """按配置间隔导出完整备份 ZIP，并清理超出保留份数的旧备份。"""
        last_run: float = 0.0
        while True:
            try:
                config = self.runtime_config()
                if config.auto_backup_enabled:
                    now = time.monotonic()
                    minimum_gap = max(
                        BACKUP_POLL_SECONDS * 2,
                        config.auto_backup_interval_hours * 3600.0,
                    )
                    if now - last_run >= minimum_gap:
                        last_run = now
                        await self._run_auto_backup(config)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("321开团自动备份异常，将在稍后重试")
            await asyncio.sleep(BACKUP_POLL_SECONDS)

    async def _run_auto_backup(self, config: Any) -> None:
        export_dir = self.data_dir / "exports"
        try:
            path = await build_backup_archive(
                data_dir=self.data_dir,
                database=self.database,
                export_dir=export_dir,
            )
        except Exception:
            logger.exception("321开团自动备份导出失败")
            return
        try:
            removed = await asyncio.to_thread(
                prune_backups,
                export_dir,
                int(config.auto_backup_keep_count),
            )
        except Exception:
            logger.exception("321开团自动备份清理失败")
            removed = []
        await self.broker.publish(
            {
                "type": "backup",
                "action": "auto",
                "path": path.name,
                "removed": [item.name for item in removed],
            }
        )
        logger.info(
            "321开团自动备份完成：%s（清理 %s 份旧备份）",
            path.name,
            len(removed),
        )

    async def _webhook_loop(self) -> None:
        """订阅事件总线，把符合配置的事件推送到外部地址。"""
        backoff = 1.0
        while True:
            try:
                self._webhook_status["state"] = "subscribed"
                async for event in self.broker.subscribe():
                    backoff = 1.0
                    if event.get("type") in {"ready", "keepalive"}:
                        continue
                    try:
                        config = self.runtime_config()
                        if (
                            not config.webhook_enabled
                            or not config.webhook_urls
                        ):
                            self._webhook_status["state"] = "disabled"
                            continue
                        event_type = str(event.get("type") or "")
                        if (
                            config.webhook_events
                            and event_type not in config.webhook_events
                        ):
                            continue
                        await self._dispatch_webhooks(config, event)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._webhook_status.update(
                            {
                                "state": "degraded",
                                "consecutive_failures": int(
                                    self._webhook_status.get(
                                        "consecutive_failures",
                                        0,
                                    )
                                    or 0
                                )
                                + 1,
                                "last_failure_at": datetime.now(
                                    timezone.utc
                                ).isoformat(timespec="seconds"),
                                "last_error": str(exc)[:300],
                            }
                        )
                        logger.exception("321开团 Webhook 推送失败")
                raise RuntimeError("Webhook 事件订阅意外结束")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._webhook_status.update(
                    {
                        "state": "retrying",
                        "consecutive_failures": int(
                            self._webhook_status.get(
                                "consecutive_failures",
                                0,
                            )
                            or 0
                        )
                        + 1,
                        "last_failure_at": datetime.now(
                            timezone.utc
                        ).isoformat(timespec="seconds"),
                        "last_error": str(exc)[:300],
                        "next_retry_seconds": backoff,
                    }
                )
                logger.exception(
                    "321开团 Webhook 分发异常，%.1f 秒后重新订阅",
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2.0)

    async def _dispatch_webhooks(
        self,
        config: Any,
        event: Mapping[str, Any],
    ) -> None:
        body = json.dumps(
            {
                "event": event.get("type"),
                "hook": event.get("hook", ""),
                "at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "data": event,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        timeout = max(
            1.0,
            min(120.0, float(config.webhook_timeout_seconds)),
        )
        secret = str(config.webhook_secret or "")
        failures: list[str] = []
        for url in config.webhook_urls:
            ok, error = await asyncio.to_thread(
                self._post_webhook,
                str(url),
                body,
                secret,
                timeout,
            )
            if not ok:
                failures.append(f"{url}: {error}")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if failures:
            self._webhook_status.update(
                {
                    "state": "degraded",
                    "consecutive_failures": int(
                        self._webhook_status.get("consecutive_failures", 0)
                        or 0
                    )
                    + 1,
                    "last_failure_at": now,
                    "last_error": "；".join(failures)[:300],
                }
            )
            logger.warning(
                "321开团 Webhook 部分地址推送失败：%s",
                "；".join(failures),
            )
        else:
            self._webhook_status.update(
                {
                    "state": "ready",
                    "consecutive_failures": 0,
                    "last_success_at": now,
                    "last_error": "",
                }
            )

    @staticmethod
    def _post_webhook(
        url: str,
        body: bytes,
        secret: str,
        timeout: float,
    ) -> tuple[bool, str]:
        delivery_id = hashlib.sha256(body).hexdigest()
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Tavern-Delivery-Id": delivery_id,
            },
        )
        if secret:
            digest = hmac.new(
                secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            request.add_header("X-Tavern-Signature", f"sha256={digest}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read(64 * 1024)
            return True, ""
        except Exception as exc:  # noqa: BLE001 - 推送失败不阻断主流程
            return False, str(exc)
