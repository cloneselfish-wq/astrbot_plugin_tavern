from __future__ import annotations

from .health_maintenance_support import *


def _release_artifact_health(plugin_root: Path, checked_at: str) -> dict[str, Any]:
    plugin_root = Path(plugin_root)
    manifest_path = plugin_root / "RELEASE_MANIFEST.json"
    if not manifest_path.is_file():
        development_source = any(
            (plugin_root / relative).exists()
            for relative in (
                "pages/console-src",
                "tests",
                "tools",
            )
        )
        return _health_component(
            "release_artifact",
            "degraded" if development_source else "blocked",
            (
                "当前为开发源码工作区，未携带安装包发布清单"
                if development_source
                else "当前为安装运行树，但发布清单缺失"
            ),
            reason=(
                "正式安装包构建时会生成并校验发布清单"
                if development_source
                else "无法校验安装成员，请从完整发布包重新安装"
            ),
            metrics={
                "manifest_present": False,
                "runtime_kind": (
                    "development_source" if development_source else "installation"
                ),
                "integrity": "not_applicable" if development_source else "missing",
            },
            checked_at=checked_at,
        )

    issues: list[str] = []
    verified_members = 0
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            raise ValueError("发布清单必须是 JSON 对象")
        if manifest.get("format") != "astrbot-plugin-runtime":
            issues.append("发布清单格式无效")
        if int(manifest.get("format_version") or 0) != 2:
            issues.append("发布清单版本无效")
        if str(manifest.get("version") or "") != PLUGIN_VERSION:
            issues.append("插件版本与发布清单不一致")
        raw_members = manifest.get("members")
        if not isinstance(raw_members, Sequence) or isinstance(
            raw_members,
            (str, bytes),
        ):
            raise ValueError("发布清单成员列表无效")
        seen_members: set[str] = set()
        for item in raw_members:
            if not isinstance(item, Mapping):
                issues.append("发布清单成员格式无效")
                continue
            relative = str(item.get("path") or "").replace("\\", "/")
            member = Path(relative)
            if (
                not relative
                or member.is_absolute()
                or ".." in member.parts
                or relative == "RELEASE_MANIFEST.json"
                or relative in seen_members
            ):
                issues.append("发布清单成员路径无效或重复")
                continue
            seen_members.add(relative)
            target = manifest_path.parent.joinpath(*member.parts)
            if not target.is_file():
                issues.append("发布清单声明的成员缺失")
                continue
            if int(item.get("bytes") or -1) != target.stat().st_size:
                issues.append("发布成员大小不一致")
                continue
            if str(item.get("sha256") or "") != _sha256_file(target):
                issues.append("发布成员 SHA-256 不一致")
                continue
            verified_members += 1
        actual_members = {
            path.relative_to(manifest_path.parent).as_posix()
            for path in manifest_path.parent.rglob("*")
            if path.is_file()
            and path != manifest_path
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        }
        if actual_members - seen_members:
            issues.append("安装目录存在发布清单未声明的成员")
        if seen_members - actual_members:
            issues.append("发布清单声明的成员不完整")
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        issues.append(_safe_summary(exc, 120))

    state = "blocked" if issues else "ready"
    return _health_component(
        "release_artifact",
        state,
        (
            f"纯安装运行树的发布清单与 {verified_members} 个成员一致"
            if not issues
            else "安装运行树的发布清单或成员已损坏"
        ),
        reason="；".join(dict.fromkeys(issues[:3])),
        metrics={
            "manifest_present": True,
            "runtime_kind": "installation",
            "integrity": "corrupt" if issues else "verified",
            "verified_members": verified_members,
            "issues": len(issues),
        },
        checked_at=checked_at,
    )


class HealthMaintenanceRuntimeRepositoryMixin:
    def _health_summary(self) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds")
        components: list[dict[str, Any]] = []
        with self._connect() as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            schema = connection.execute(
                "SELECT value FROM tavern_meta WHERE key='schema_version'"
            ).fetchone()
            schema_value = int(schema["value"] if schema else 0)
            db_state = (
                "ready"
                if schema_value == DATABASE_SCHEMA_VERSION
                and quick
                and str(quick[0]).lower() == "ok"
                and not foreign
                else "blocked"
            )
            components.append(
                _health_component(
                    "database",
                    db_state,
                    (
                        f"Schema {schema_value}，SQLite 与外键检查通过"
                        if db_state == "ready"
                        else "数据库结构或完整性检查未通过"
                    ),
                    reason=(
                        ""
                        if db_state == "ready"
                        else "Schema、quick_check 或外键存在异常"
                    ),
                    metrics={
                        "schema": schema_value,
                        "quick_check": (
                            str(quick[0]).lower() if quick else "missing"
                        ),
                        "foreign_key_violations": len(foreign),
                    },
                    checked_at=now,
                )
            )

            maintenance_meta = connection.execute(
                "SELECT value FROM tavern_meta WHERE key='maintenance_mode'"
            ).fetchone()
            maintenance_sessions = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM sessions "
                    "WHERE state='maintenance'"
                ).fetchone()["count"]
            )
            maintenance = (
                str(maintenance_meta["value"] if maintenance_meta else "0")
                .strip()
                .lower()
                in {"1", "true", "yes", "on"}
                or maintenance_sessions > 0
            )
            migration_row = connection.execute(
                """
                SELECT * FROM migration_receipts
                WHERE migration_type='schema'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
            migration_reason = ""
            migration_metrics: dict[str, Any] = {
                "receipt_present": bool(migration_row),
                "maintenance_sessions": maintenance_sessions,
            }
            if maintenance:
                migration_state = "maintenance"
                migration_summary = "系统处于维护窗口，新的业务写入应保持暂停"
            elif schema_value != DATABASE_SCHEMA_VERSION:
                migration_state = "blocked"
                migration_summary = "数据库迁移尚未达到当前 Schema"
                migration_reason = (
                    f"当前 Schema {schema_value}，目标 Schema "
                    f"{DATABASE_SCHEMA_VERSION}"
                )
            elif migration_row is None:
                migration_state = "ready"
                migration_summary = "当前数据库无需迁移，Schema 已就绪"
            else:
                receipt = json_load(migration_row["receipt_json"], {})
                receipt = receipt if isinstance(receipt, Mapping) else {}
                source_schema = int(receipt.get("source_schema") or 0)
                target_schema = int(receipt.get("target_schema") or 0)
                integrity_ok = (
                    str(receipt.get("integrity_check") or "").lower() == "ok"
                    and int(receipt.get("foreign_key_violations") or 0) == 0
                )
                migration_metrics.update(
                    {
                        "source_schema": source_schema,
                        "target_schema": target_schema,
                        "integrity_verified": integrity_ok,
                    }
                )
                if target_schema != DATABASE_SCHEMA_VERSION or not integrity_ok:
                    migration_state = "blocked"
                    migration_summary = "最近迁移回执不完整或校验失败"
                    migration_reason = "迁移目标、完整性或外键校验与当前版本不一致"
                else:
                    migration_state = "ready"
                    migration_summary = (
                        f"Schema {source_schema}→{target_schema} 迁移回执有效"
                    )
            components.append(
                _health_component(
                    "migration",
                    migration_state,
                    migration_summary,
                    reason=migration_reason,
                    metrics=migration_metrics,
                    checked_at=now,
                )
            )

            for table, code, label in (
                ("delivery_outbox", "delivery_outbox", "消息投递"),
                ("storage_sync_outbox", "storage_outbox", "副本存储"),
                ("event_outbox", "event_outbox", "事件投递"),
            ):
                statuses = {
                    str(row["status"]): int(row["count"])
                    for row in connection.execute(
                        f"""
                        SELECT status, COUNT(*) AS count
                        FROM {table} GROUP BY status
                        """
                    ).fetchall()
                }
                oldest = connection.execute(
                    f"""
                    SELECT MIN(created_at) AS created_at FROM {table}
                    WHERE status IN (
                        'pending', 'leased', 'partially_sent', 'retry_wait'
                    )
                    """
                ).fetchone()
                age = _age_seconds(
                    str(oldest["created_at"] or "") if oldest else "",
                    now_dt,
                )
                permanent = statuses.get("permanently_failed", 0)
                state = timed_state(
                    age,
                    has_retry=bool(statuses.get("retry_wait", 0)),
                    has_permanent_failure=bool(permanent),
                    threshold=OUTBOX_THRESHOLD,
                )
                waiting = sum(
                    statuses.get(key, 0)
                    for key in (
                        "pending",
                        "leased",
                        "partially_sent",
                        "retry_wait",
                    )
                )
                components.append(
                    _health_component(
                        code,
                        state,
                        (
                            f"{waiting} 项处理中，最久等待 {age} 秒"
                            if waiting
                            else f"{label}当前没有待处理项目"
                        ),
                        reason=(
                            f"{permanent} 项达到重试上限"
                            if permanent
                            else (
                                "存在自动重试或等待时间达到健康阈值"
                                if state == "degraded"
                                else (
                                    "最久等待时间超过阻断阈值"
                                    if state == "blocked"
                                    else ""
                                )
                            )
                        ),
                        metrics={
                            **statuses,
                            "oldest_age_seconds": age,
                            "degraded_after_seconds": (
                                OUTBOX_THRESHOLD.degraded_after_seconds
                            ),
                            "blocked_after_seconds": (
                                OUTBOX_THRESHOLD.blocked_after_seconds
                            ),
                        },
                        checked_at=now,
                    )
                )

            projection_rows = connection.execute(
                """
                SELECT
                    s.id AS session_id,
                    COALESCE(MAX(e.seq), 0) AS latest_seq,
                    (
                        SELECT COUNT(*) FROM player_tendency_evidence te
                        WHERE te.session_id=s.id
                    ) AS tendency_evidence,
                    (
                        SELECT COUNT(*) FROM player_tendency_profiles tp
                        WHERE tp.session_id=s.id
                    ) AS tendency_profiles,
                    (
                        SELECT COUNT(*) FROM npc_knowledge_evidence nk
                        WHERE nk.session_id=s.id
                    ) AS knowledge_evidence,
                    (
                        SELECT COUNT(*) FROM session_characters sc
                        WHERE sc.session_id=s.id
                          AND (
                            sc.known_facts_json<>'[]'
                            OR sc.misconceptions_json<>'[]'
                          )
                    ) AS knowledge_cache
                FROM sessions s
                LEFT JOIN session_events e ON e.session_id=s.id
                GROUP BY s.id
                """
            ).fetchall()
            projection_names = ("player_tendency", "npc_knowledge")
            projection_lags: list[int] = []
            projection_missing = 0
            projection_failed = 0
            projection_sessions = 0
            for row in projection_rows:
                needed = {
                    "player_tendency": bool(
                        int(row["tendency_evidence"] or 0)
                        or int(row["tendency_profiles"] or 0)
                    ),
                    "npc_knowledge": bool(
                        int(row["knowledge_evidence"] or 0)
                        or int(row["knowledge_cache"] or 0)
                    ),
                }
                if not any(needed.values()):
                    continue
                projection_sessions += 1
                checkpoints = {
                    str(item["projection_name"]): item
                    for item in connection.execute(
                        """
                        SELECT * FROM projection_checkpoints
                        WHERE session_id=?
                        """,
                        (row["session_id"],),
                    ).fetchall()
                }
                for projection_name in projection_names:
                    if not needed[projection_name]:
                        continue
                    checkpoint = checkpoints.get(projection_name)
                    if checkpoint is None:
                        projection_missing += 1
                        continue
                    lag = max(
                        0,
                        int(row["latest_seq"] or 0)
                        - int(checkpoint["last_seq"] or 0),
                    )
                    projection_lags.append(lag)
                    payload = json_load(checkpoint["payload_json"], {})
                    payload = payload if isinstance(payload, Mapping) else {}
                    if (
                        str(payload.get("status") or "").lower()
                        in {"failed", "blocked", "gap"}
                        or payload.get("failed") is True
                    ):
                        projection_failed += 1
            max_projection_lag = max(projection_lags, default=0)
            projection_health = projection_state(
                max_projection_lag,
                failed=bool(projection_missing or projection_failed),
            )
            components.append(
                _health_component(
                    "projection",
                    projection_health,
                    (
                        "当前没有需要追赶的倾向或 NPC 知识投影"
                        if projection_sessions == 0
                        else (
                            f"{projection_sessions} 个副本已检查，"
                            f"最大滞后 {max_projection_lag} 个事件"
                        )
                    ),
                    reason=(
                        (
                            f"缺少 {projection_missing} 个必要检查点；"
                            f"{projection_failed} 个检查点报告失败"
                        )
                        if projection_missing or projection_failed
                        else (
                            "投影滞后达到健康阈值"
                            if projection_health != "ready"
                            else ""
                        )
                    ),
                    metrics={
                        "sessions_checked": projection_sessions,
                        "max_lag": max_projection_lag,
                        "missing_checkpoints": projection_missing,
                        "failed_checkpoints": projection_failed,
                    },
                    checked_at=now,
                )
            )

            operation_status = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM operation_receipts GROUP BY status
                    """
                ).fetchall()
            }
            active_operation_statuses = (
                "reserved",
                "generating",
                "dice_locked",
                "ready_to_commit",
            )
            placeholders = ",".join("?" for _ in active_operation_statuses)
            expired_operations = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) AS count FROM operation_receipts
                    WHERE status IN ({placeholders})
                      AND lease_expires_at<>'' AND lease_expires_at<=?
                    """,
                    (*active_operation_statuses, now),
                ).fetchone()["count"]
            )
            recovery_limit = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM operation_receipts
                    WHERE retry_count>=?
                    """,
                    (OPERATION_RECOVERY_FAILURE_LIMIT,),
                ).fetchone()["count"]
            )
            incomplete_commits = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM operation_commits
                    WHERE status<>'completed'
                    """
                ).fetchone()["count"]
            )
            needs_recovery = operation_status.get("needs_recovery", 0)
            failed_retryable = operation_status.get("failed_retryable", 0)
            if needs_recovery or recovery_limit:
                operations_state = "blocked"
            elif expired_operations or failed_retryable or incomplete_commits:
                operations_state = "degraded"
            else:
                operations_state = "ready"
            components.append(
                _health_component(
                    "operations",
                    operations_state,
                    (
                        "操作回执与提交记录没有待恢复项目"
                        if operations_state == "ready"
                        else (
                            f"{expired_operations} 个过期租约，"
                            f"{failed_retryable} 个可重试操作，"
                            f"{needs_recovery} 个需要恢复"
                        )
                    ),
                    reason=(
                        f"{recovery_limit} 个操作达到 "
                        f"{OPERATION_RECOVERY_FAILURE_LIMIT} 次恢复失败上限"
                        if recovery_limit
                        else (
                            "存在需要人工确认的恢复状态"
                            if needs_recovery
                            else ""
                        )
                    ),
                    metrics={
                        **operation_status,
                        "expired_leases": expired_operations,
                        "recovery_failure_limit": (
                            OPERATION_RECOVERY_FAILURE_LIMIT
                        ),
                        "at_recovery_limit": recovery_limit,
                        "incomplete_commits": incomplete_commits,
                    },
                    checked_at=now,
                )
            )

            job_status = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM author_jobs GROUP BY status
                    """
                ).fetchall()
            }
            oldest_job = connection.execute(
                """
                SELECT MIN(created_at) AS created_at FROM author_jobs
                WHERE status='queued'
                """
            ).fetchone()
            job_age = _age_seconds(
                str(oldest_job["created_at"] or "") if oldest_job else "",
                now_dt,
            )
            job_permanent = job_status.get("permanently_failed", 0)
            expired_job_rows = connection.execute(
                """
                SELECT lease_expires_at FROM author_jobs
                WHERE status IN ('leased', 'running')
                  AND lease_expires_at<>'' AND lease_expires_at<=?
                """,
                (now,),
            ).fetchall()
            expired_job_leases = len(expired_job_rows)
            oldest_expired_job = max(
                (
                    _age_seconds(str(row["lease_expires_at"] or ""), now_dt)
                    for row in expired_job_rows
                ),
                default=0,
            )
            stale_job_leases = sum(
                1
                for row in expired_job_rows
                if _age_seconds(str(row["lease_expires_at"] or ""), now_dt)
                > AUTHOR_LEASE_BLOCKED_SECONDS
            )
            job_state = timed_state(
                job_age,
                has_retry=bool(
                    job_status.get("retry_wait", 0) or expired_job_leases
                ),
                has_permanent_failure=bool(
                    job_permanent or stale_job_leases
                ),
                threshold=AUTHOR_JOB_THRESHOLD,
            )
            components.append(
                _health_component(
                    "author_jobs",
                    job_state,
                    (
                        f"{sum(job_status.get(k, 0) for k in AUTHOR_ACTIVE_STATUSES)} "
                        f"项处理中，最久 {job_age} 秒"
                    ),
                    reason=(
                        f"{job_permanent} 项永久失败，"
                        f"{stale_job_leases} 项租约失联超过 "
                        f"{AUTHOR_LEASE_BLOCKED_SECONDS} 秒"
                        if job_permanent or stale_job_leases
                        else (
                            f"{expired_job_leases} 项租约刚过期，系统可安全回收"
                            if expired_job_leases
                            else ""
                        )
                    ),
                    metrics={
                        **job_status,
                        "oldest_age_seconds": job_age,
                        "expired_leases": expired_job_leases,
                        "oldest_expired_lease_seconds": oldest_expired_job,
                        "stale_leases": stale_job_leases,
                        "lease_blocked_after_seconds": (
                            AUTHOR_LEASE_BLOCKED_SECONDS
                        ),
                    },
                    checked_at=now,
                )
            )

            from ..builtin_worlds import (
                builtin_world_specs,
                resolve_builtin_archive,
            )
            from ..protocol.references import inspect_twp_archive
            from ..twp.validation.privacy import check_template

            plugin_root = Path(__file__).resolve().parents[2]
            world_failures: list[str] = []
            world_warnings = 0
            world_checked = 0
            for spec in builtin_world_specs():
                row = connection.execute(
                    """
                    SELECT * FROM worlds
                    WHERE slug=? AND source_package_id=?
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (spec.slug, spec.package_id),
                ).fetchone()
                if row is None:
                    world_failures.append(f"{spec.display_name}尚未写入世界库")
                    continue
                archive_path = resolve_builtin_archive(plugin_root, spec)
                if not archive_path.is_file():
                    world_failures.append(f"{spec.display_name}缺少内置世界归档")
                    continue
                try:
                    archive_report = inspect_twp_archive(archive_path)
                except (OSError, TypeError, ValueError, zipfile.BadZipFile) as exc:
                    world_failures.append(
                        f"{spec.display_name}归档无法检查：{_safe_summary(exc, 80)}"
                    )
                    continue
                compiled = archive_report.get("compiled_world")
                compiled = compiled if isinstance(compiled, Mapping) else {}
                archive_hash = str(archive_report.get("artifact_hash") or "")
                identity_ok = all(
                    (
                        bool(archive_report.get("compatible")),
                        str(compiled.get("package_id") or "") == spec.package_id,
                        str(compiled.get("slug") or "") == spec.slug,
                        str(compiled.get("content_version") or "")
                        == spec.content_version,
                        str(row["source_package_id"] or "") == spec.package_id,
                        str(row["slug"] or "") == spec.slug,
                        str(row["content_version"] or "")
                        == spec.content_version,
                        bool(archive_hash),
                        str(row["source_artifact_hash"] or "") == archive_hash,
                    )
                )
                world = self._world(row)
                character_rows = connection.execute(
                    """
                    SELECT * FROM characters
                    WHERE world_id=? AND enabled=1
                    ORDER BY sort_order, name COLLATE NOCASE
                    """,
                    (row["id"],),
                ).fetchall()
                world["characters"] = [
                    self._character(character) for character in character_rows
                ]
                report = check_template(world)
                world_checked += 1
                world_warnings += len(report.get("warnings") or [])
                if not identity_ok:
                    world_failures.append(
                        f"{spec.display_name}的包身份、版本或内容校验不一致"
                    )
                elif not report.get("compatible"):
                    world_failures.append(
                        f"{spec.display_name}存在世界契约阻断问题"
                    )
            expected_worlds = len(builtin_world_specs())
            world_state = "blocked" if world_failures else (
                "ready" if world_checked == expected_worlds else "blocked"
            )
            components.append(
                _health_component(
                    "world_integrity",
                    world_state,
                    (
                        f"{world_checked}/{expected_worlds} 个内置世界身份、"
                        "Artifact 与模板契约一致"
                        if world_state == "ready"
                        else f"{len(world_failures)} 个内置世界问题需要处理"
                    ),
                    reason="；".join(world_failures[:3]),
                    metrics={
                        "expected_worlds": expected_worlds,
                        "verified_worlds": world_checked,
                        "failures": len(world_failures),
                        "warnings": world_warnings,
                    },
                    checked_at=now,
                )
            )

            latest_configuration = connection.execute(
                """
                SELECT payload_json FROM configuration_revisions
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            configuration = (
                json_load(latest_configuration["payload_json"], {})
                if latest_configuration
                else {}
            )
            configuration = (
                configuration if isinstance(configuration, Mapping) else {}
            )
            providers = _configured_provider_ids(configuration)
            active_ai_sessions = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM sessions
                    WHERE state IN ('preparing', 'running')
                    """
                ).fetchone()["count"]
            )
            provider_rows = {
                str(row["provider_id"]): row
                for row in connection.execute(
                    "SELECT * FROM provider_health"
                ).fetchall()
            }
            provider_open = 0
            provider_unknown = 0
            provider_healthy = 0
            for provider_id in providers:
                row = provider_rows.get(provider_id)
                if row is None:
                    provider_unknown += 1
                elif str(row["status"] or "") == "healthy":
                    provider_healthy += 1
                else:
                    provider_open += 1
            if not providers:
                provider_state = "ready"
                provider_summary = "未固定模型服务，运行时使用宿主当前会话选择"
                provider_reason = ""
            elif not active_ai_sessions:
                provider_state = "ready"
                provider_summary = (
                    f"已配置 {len(providers)} 个模型服务，当前没有运行中的 AI 需求"
                )
                provider_reason = ""
            elif provider_healthy:
                provider_state = (
                    "degraded" if provider_open or provider_unknown else "ready"
                )
                provider_summary = (
                    f"{provider_healthy} 个服务可用，"
                    f"{provider_open} 个不可用，{provider_unknown} 个尚无探测记录"
                )
                provider_reason = (
                    "主服务异常时仅使用已声明且可用的回退服务"
                    if provider_state == "degraded"
                    else ""
                )
            elif provider_open and not provider_unknown:
                provider_state = "blocked"
                provider_summary = "当前玩法需要的模型服务均不可用"
                provider_reason = "所有已声明服务的健康状态均为断路或半开"
            else:
                provider_state = "degraded"
                provider_summary = "模型服务尚无足够的健康探测记录"
                provider_reason = "存在已配置但未记录成功或失败的服务"
            components.append(
                _health_component(
                    "provider_health",
                    provider_state,
                    provider_summary,
                    reason=provider_reason,
                    metrics={
                        "configured": len(providers),
                        "healthy": provider_healthy,
                        "unavailable": provider_open,
                        "unknown": provider_unknown,
                        "active_ai_sessions": active_ai_sessions,
                    },
                    checked_at=now,
                )
            )

        exports_dir = Path(self.data_dir) / "exports"
        backup_candidates = sorted(
            (
                path
                for path in exports_dir.glob("backup_tavern*.zip")
                if path.is_file()
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        backup_state = "blocked"
        backup_summary = "没有可验证的完整备份"
        backup_reason = "请创建一份新备份并保留旧文件"
        backup_metrics: dict[str, Any] = {
            "archive_present": False,
            "integrity": "missing",
            "verified": False,
            "age_seconds": None,
        }
        if backup_candidates:
            latest_backup = backup_candidates[0]
            try:
                from ..runtime.recovery_service import verify_backup_archive

                bundle, checksums = verify_backup_archive(latest_backup)
                expected_database_sha = str(
                    bundle.get("database_sha256") or ""
                )
                if (
                    not expected_database_sha
                    or checksums.get("catalog.sqlite3")
                    != expected_database_sha
                ):
                    raise ValueError("备份数据库 SHA-256 与清单不一致")
                with tempfile.TemporaryDirectory(
                    prefix="tavern-health-backup-"
                ) as temporary:
                    catalog = Path(temporary) / "catalog.sqlite3"
                    with zipfile.ZipFile(latest_backup) as archive:
                        with archive.open("catalog.sqlite3") as source:
                            with catalog.open("wb") as target:
                                for chunk in iter(
                                    lambda: source.read(1024 * 1024),
                                    b"",
                                ):
                                    target.write(chunk)
                    if _sha256_file(catalog) != expected_database_sha:
                        raise ValueError("备份数据库解包后 SHA-256 不一致")
                    with closing(sqlite3.connect(catalog)) as backup_connection:
                        backup_connection.execute("PRAGMA foreign_keys=ON")
                        backup_quick = backup_connection.execute(
                            "PRAGMA quick_check"
                        ).fetchone()
                        backup_foreign = backup_connection.execute(
                            "PRAGMA foreign_key_check"
                        ).fetchall()
                        backup_schema_row = backup_connection.execute(
                            """
                            SELECT value FROM tavern_meta
                            WHERE key='schema_version'
                            """
                        ).fetchone()
                    backup_schema = int(
                        backup_schema_row[0] if backup_schema_row else 0
                    )
                    if (
                        backup_quick is None
                        or str(backup_quick[0]).lower() != "ok"
                        or backup_foreign
                        or backup_schema != int(bundle.get("schema_version") or 0)
                    ):
                        raise ValueError("备份 SQLite 完整性、外键或 Schema 校验失败")
                created_at = _utc(str(bundle.get("created_at") or ""))
                backup_age = (
                    max(0, int((now_dt - created_at).total_seconds()))
                    if created_at is not None
                    else max(
                        0,
                        int(now_dt.timestamp() - latest_backup.stat().st_mtime),
                    )
                )
                if backup_age > BACKUP_BLOCKED_SECONDS:
                    backup_state = "blocked"
                    backup_reason = "最近备份超过七天，不能作为当前恢复保障"
                elif backup_age >= BACKUP_DEGRADED_SECONDS:
                    backup_state = "degraded"
                    backup_reason = "最近备份超过二十四小时，建议立即创建新备份"
                else:
                    backup_state = "ready"
                    backup_reason = ""
                backup_summary = (
                    f"最近完整备份已验证，距今 {backup_age} 秒"
                )
                backup_metrics = {
                    "archive_present": True,
                    "integrity": "verified",
                    "verified": True,
                    "age_seconds": backup_age,
                    "schema": backup_schema,
                    "verified_members": len(checksums),
                    "degraded_after_seconds": BACKUP_DEGRADED_SECONDS,
                    "blocked_after_seconds": BACKUP_BLOCKED_SECONDS,
                }
            except (
                OSError,
                sqlite3.Error,
                TypeError,
                ValueError,
                zipfile.BadZipFile,
            ) as exc:
                backup_state = "blocked"
                backup_summary = "最近完整备份未通过校验"
                backup_reason = _safe_summary(exc, 160)
                backup_metrics = {
                    "archive_present": True,
                    "integrity": "corrupt",
                    "verified": False,
                    "age_seconds": None,
                }
        components.append(
            _health_component(
                "backup",
                backup_state,
                backup_summary,
                reason=backup_reason,
                metrics=backup_metrics,
                checked_at=now,
            )
        )

        components.append(
            _release_artifact_health(
                Path(__file__).resolve().parents[2],
                now,
            )
        )

        rank = {"ready": 0, "degraded": 1, "blocked": 2}
        overall = max(
            (item["state"] for item in components),
            key=lambda item: rank.get(item, 2),
            default="ready",
        )
        if maintenance:
            overall = "maintenance"
        return {
            "schema": "tavern-health-summary/1.0.0-rc10",
            "generated_at": now,
            "overall": overall,
            "maintenance": bool(maintenance),
            "components": components,
        }
