"""Durable two-stage receipts for RC8 GitHub world imports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..database_support import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    clean_text,
    json_dump,
    json_load,
    utc_now,
)
from ..resolution_receipts import content_hash


def _actor_scope(actor_id: str) -> str:
    return f"github:{content_hash({'actor': str(actor_id)})[:32]}"


def _operation_id(kind: str, actor_id: str, idempotency_key: str) -> str:
    digest = content_hash(
        {
            "scope": f"github.import.{kind}",
            "actor": str(actor_id),
            "idempotency_key": str(idempotency_key),
        }
    )
    return f"github-import-{kind}:{digest[:40]}"


def _preview_revision(candidates: Sequence[Mapping[str, Any]]) -> int:
    return int(content_hash({"candidates": list(candidates)})[:13], 16)


class GithubImportRepositoryMixin:
    async def prepare_github_import_preview(
        self,
        actor_id: str,
        *,
        repo_url: str,
        branch: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._prepare_github_import_preview,
            str(actor_id),
            str(repo_url),
            str(branch),
            str(idempotency_key),
        )

    def _prepare_github_import_preview(
        self,
        actor_id: str,
        repo_url: str,
        branch: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        repo_url = clean_text(repo_url, max_chars=500)
        branch = clean_text(branch, max_chars=160)
        key = clean_text(idempotency_key, max_chars=200)
        if not actor_id or not repo_url or not key:
            raise ValueError("GitHub 扫描缺少仓库地址或防重复凭证")
        request = {
            "operation_type": "github.import.preview",
            "repo_url": repo_url,
            "branch": branch,
        }
        input_hash = content_hash(request)
        operation_id = _operation_id("preview", actor_id, key)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["session_id"] or "") != _actor_scope(actor_id):
                        raise DatabaseNotFoundError("GitHub 扫描预览不存在")
                    if str(receipt["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "相同防重复凭证已用于另一个 GitHub 仓库"
                        )
                    if str(receipt["status"] or "") == "completed":
                        result = json_load(receipt["result_json"], {})
                        result = dict(result) if isinstance(result, Mapping) else {}
                        result.update(
                            {"operation_id": operation_id, "replayed": True}
                        )
                        connection.execute("COMMIT")
                        return result
                    if str(receipt["status"] or "") not in {
                        "reserved", "failed_retryable"
                    }:
                        raise DatabaseConflictError(
                            "GitHub 扫描不能从当前状态继续"
                        )
                    connection.execute(
                        """
                        UPDATE operation_receipts
                        SET status='reserved', phase='scan_pending',
                            last_error_code='', updated_at=?
                        WHERE operation_id=?
                        """,
                        (now, operation_id),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO operation_receipts(
                            operation_id, session_id, operation_type,
                            request_json, result_json, status, phase,
                            input_hash, created_at, updated_at
                        ) VALUES (?, ?, 'github.import.preview', ?, '{}',
                                  'reserved', 'scan_pending', ?, ?, ?)
                        """,
                        (
                            operation_id,
                            _actor_scope(actor_id),
                            json_dump(request),
                            input_hash,
                            now,
                            now,
                        ),
                    )
                connection.execute("COMMIT")
                return {
                    "operation_id": operation_id,
                    "state": "prepared",
                    "replayed": False,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def complete_github_import_preview(
        self,
        actor_id: str,
        *,
        repo_url: str,
        branch: str,
        resolved_branch: str,
        idempotency_key: str,
        owner_label: str,
        repository_label: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return await self._run(
            self._complete_github_import_preview,
            str(actor_id),
            str(repo_url),
            str(branch),
            str(resolved_branch),
            str(idempotency_key),
            str(owner_label),
            str(repository_label),
            [dict(item) for item in candidates],
        )

    def _complete_github_import_preview(
        self,
        actor_id: str,
        repo_url: str,
        branch: str,
        resolved_branch: str,
        idempotency_key: str,
        owner_label: str,
        repository_label: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        request = {
            "operation_type": "github.import.preview",
            "repo_url": clean_text(repo_url, max_chars=500),
            "branch": clean_text(branch, max_chars=160),
        }
        input_hash = content_hash(request)
        operation_id = _operation_id("preview", actor_id, idempotency_key)
        normalized: list[dict[str, str]] = []
        for item in candidates[:100]:
            name = clean_text(item.get("name"), max_chars=200)
            url = clean_text(item.get("url"), max_chars=1000)
            if not name or not url:
                continue
            normalized.append(
                {
                    "name": name,
                    "url": url,
                    "source": clean_text(item.get("source"), max_chars=40),
                    "folder": clean_text(item.get("folder"), max_chars=240),
                }
            )
        if not normalized:
            raise ValueError("GitHub 仓库没有可导入的 ZIP 世界包")
        revision = _preview_revision(normalized)
        result = {
            "owner_label": clean_text(owner_label, max_chars=100),
            "repository_label": clean_text(repository_label, max_chars=160),
            "branch_label": clean_text(resolved_branch or branch, max_chars=160),
            "candidates": normalized,
            "revision": revision,
            "replayed": False,
        }
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is None or str(receipt["session_id"] or "") != _actor_scope(actor_id):
                    raise DatabaseNotFoundError("GitHub 扫描预留不存在")
                if str(receipt["input_hash"] or "") != input_hash:
                    raise DatabaseConflictError("GitHub 扫描预留与结果不匹配")
                if str(receipt["status"] or "") == "completed":
                    replay = json_load(receipt["result_json"], {})
                    replay = dict(replay) if isinstance(replay, Mapping) else {}
                    replay.update({"operation_id": operation_id, "replayed": True})
                    connection.execute("COMMIT")
                    return replay
                if str(receipt["status"] or "") not in {"reserved", "failed_retryable"}:
                    raise DatabaseConflictError("GitHub 扫描不能提交结果")
                connection.execute(
                    """
                    UPDATE operation_receipts
                    SET result_json=?, status='completed', phase='previewed',
                        committed_revision=?, updated_at=?
                    WHERE operation_id=?
                    """,
                    (json_dump(result), revision, now, operation_id),
                )
                connection.execute("COMMIT")
                return {**result, "operation_id": operation_id}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def fail_github_import_preview(
        self,
        actor_id: str,
        *,
        idempotency_key: str,
        error_code: str,
    ) -> None:
        await self._run(
            self._fail_github_import_preview,
            str(actor_id), str(idempotency_key), str(error_code)
        )

    def _fail_github_import_preview(
        self, actor_id: str, idempotency_key: str, error_code: str
    ) -> None:
        operation_id = _operation_id("preview", actor_id, idempotency_key)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE operation_receipts
                SET status='failed_retryable', phase='scan_failed',
                    last_error_code=?, updated_at=?
                WHERE operation_id=? AND session_id=? AND status!='completed'
                """,
                (
                    clean_text(error_code, max_chars=120),
                    utc_now(),
                    operation_id,
                    _actor_scope(actor_id),
                ),
            )

    async def list_github_import_previews(
        self, actor_id: str, *, limit: int = 5
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_github_import_previews, str(actor_id), int(limit)
        )

    def _list_github_import_previews(
        self, actor_id: str, limit: int
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT operation_id, result_json, updated_at
                FROM operation_receipts
                WHERE operation_type='github.import.preview' AND session_id=?
                  AND status='completed' AND phase='previewed'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (_actor_scope(actor_id), max(1, min(20, limit))),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            result = json_load(row["result_json"], {})
            if isinstance(result, Mapping):
                output.append(
                    {
                        **dict(result),
                        "operation_id": str(row["operation_id"]),
                        "updated_at": str(row["updated_at"] or ""),
                    }
                )
        return output

    async def prepare_github_import_commit(
        self,
        actor_id: str,
        *,
        preview_operation_id: str,
        candidate_index: int,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._prepare_github_import_commit,
            str(actor_id),
            str(preview_operation_id),
            int(candidate_index),
            int(expected_revision),
            str(idempotency_key),
        )

    def _prepare_github_import_commit(
        self,
        actor_id: str,
        preview_operation_id: str,
        candidate_index: int,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        key = clean_text(idempotency_key, max_chars=200)
        operation_id = _operation_id("commit", actor_id, key)
        request = {
            "operation_type": "github.import.commit",
            "preview_operation_id": preview_operation_id,
            "candidate_index": candidate_index,
            "expected_revision": expected_revision,
        }
        input_hash = content_hash(request)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["session_id"] or "") != _actor_scope(actor_id):
                        raise DatabaseNotFoundError("GitHub 导入回执不存在")
                    if str(receipt["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "相同防重复凭证已用于另一个导入候选"
                        )
                    if str(receipt["status"] or "") == "completed":
                        result = json_load(receipt["result_json"], {})
                        result = dict(result) if isinstance(result, Mapping) else {}
                        result["replayed"] = True
                        connection.execute("COMMIT")
                        return result
                    if str(receipt["status"] or "") not in {
                        "reserved", "failed_retryable"
                    }:
                        raise DatabaseConflictError("GitHub 导入不能从当前状态继续")
                preview = connection.execute(
                    """
                    SELECT * FROM operation_receipts
                    WHERE operation_id=? AND operation_type='github.import.preview'
                      AND session_id=? AND status='completed'
                    """,
                    (preview_operation_id, _actor_scope(actor_id)),
                ).fetchone()
                if preview is None:
                    raise DatabaseNotFoundError("GitHub 导入预览不存在")
                if str(preview["phase"] or "") != "previewed":
                    raise DatabaseConflictError("GitHub 导入预览已经使用或失效")
                preview_result = json_load(preview["result_json"], {})
                preview_result = (
                    dict(preview_result)
                    if isinstance(preview_result, Mapping)
                    else {}
                )
                if int(preview_result.get("revision") or -1) != expected_revision:
                    raise DatabaseConflictError("GitHub 导入预览已经变化")
                candidates = preview_result.get("candidates") or []
                if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
                    raise DatabaseConflictError("GitHub 导入候选无法恢复")
                if candidate_index < 0 or candidate_index >= len(candidates):
                    raise ValueError("请选择预览中仍然存在的世界包")
                candidate = candidates[candidate_index]
                if not isinstance(candidate, Mapping):
                    raise ValueError("所选世界包候选无效")
                if receipt is None:
                    connection.execute(
                        """
                        INSERT INTO operation_receipts(
                            operation_id, session_id, operation_type,
                            request_json, result_json, status, phase,
                            input_hash, created_at, updated_at
                        ) VALUES (?, ?, 'github.import.commit', ?, '{}',
                                  'reserved', 'download_pending', ?, ?, ?)
                        """,
                        (
                            operation_id,
                            _actor_scope(actor_id),
                            json_dump(request),
                            input_hash,
                            now,
                            now,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE operation_receipts
                        SET status='reserved', phase='download_pending',
                            last_error_code='', updated_at=?
                        WHERE operation_id=?
                        """,
                        (now, operation_id),
                    )
                connection.execute("COMMIT")
                return {
                    "operation_id": operation_id,
                    "preview_operation_id": preview_operation_id,
                    "candidate": dict(candidate),
                    "replayed": False,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def complete_github_import_commit(
        self,
        actor_id: str,
        *,
        preview_operation_id: str,
        candidate_index: int,
        expected_revision: int,
        idempotency_key: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._run(
            self._complete_github_import_commit,
            str(actor_id),
            str(preview_operation_id),
            int(candidate_index),
            int(expected_revision),
            str(idempotency_key),
            dict(result),
        )

    def _complete_github_import_commit(
        self,
        actor_id: str,
        preview_operation_id: str,
        candidate_index: int,
        expected_revision: int,
        idempotency_key: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        operation_id = _operation_id("commit", actor_id, idempotency_key)
        request = {
            "operation_type": "github.import.commit",
            "preview_operation_id": preview_operation_id,
            "candidate_index": candidate_index,
            "expected_revision": expected_revision,
        }
        input_hash = content_hash(request)
        safe_result = {
            "label": clean_text(result.get("label"), max_chars=200),
            "state": "已安装",
            "revision": int(result.get("revision") or 0),
            "mode": clean_text(result.get("mode"), max_chars=40),
            "replayed": False,
        }
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is None or str(receipt["session_id"] or "") != _actor_scope(actor_id):
                    raise DatabaseNotFoundError("GitHub 导入预留不存在")
                if str(receipt["input_hash"] or "") != input_hash:
                    raise DatabaseConflictError("GitHub 导入预留与结果不匹配")
                if str(receipt["status"] or "") == "completed":
                    replay = json_load(receipt["result_json"], {})
                    replay = dict(replay) if isinstance(replay, Mapping) else {}
                    replay["replayed"] = True
                    connection.execute("COMMIT")
                    return replay
                connection.execute(
                    """
                    UPDATE operation_receipts
                    SET result_json=?, status='completed', phase='committed',
                        committed_revision=?, updated_at=?
                    WHERE operation_id=?
                    """,
                    (
                        json_dump(safe_result),
                        safe_result["revision"],
                        now,
                        operation_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE operation_receipts SET phase='consumed', updated_at=?
                    WHERE operation_id=? AND session_id=?
                    """,
                    (now, preview_operation_id, _actor_scope(actor_id)),
                )
                connection.execute("COMMIT")
                return safe_result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def fail_github_import_commit(
        self,
        actor_id: str,
        *,
        idempotency_key: str,
        error_code: str,
        retryable: bool,
    ) -> None:
        await self._run(
            self._fail_github_import_commit,
            str(actor_id), str(idempotency_key), str(error_code), bool(retryable)
        )

    def _fail_github_import_commit(
        self,
        actor_id: str,
        idempotency_key: str,
        error_code: str,
        retryable: bool,
    ) -> None:
        operation_id = _operation_id("commit", actor_id, idempotency_key)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE operation_receipts
                SET status=?, phase='external_failed', last_error_code=?, updated_at=?
                WHERE operation_id=? AND session_id=? AND status!='completed'
                """,
                (
                    "failed_retryable" if retryable else "failed",
                    clean_text(error_code, max_chars=120),
                    utc_now(),
                    operation_id,
                    _actor_scope(actor_id),
                ),
            )


__all__ = ["GithubImportRepositoryMixin"]
