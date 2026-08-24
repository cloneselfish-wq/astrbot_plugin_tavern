"""SQLite store facade composed from domain repository mixins."""

from ..database_support import *

from ..legacy_reset import LegacyResetError, LegacyResetResult, backup_and_remove_legacy


import re


def _read_schema_version(path: Path) -> int:
    """Read the catalog version without loading retired migration modules."""
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT value FROM tavern_meta WHERE key='schema_version'"
        ).fetchone()
    return int(row[0]) if row else 0


_WRITE_SQL_RE = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+[A-Z]+\s+)?INTO|UPDATE|REPLACE\s+INTO|"
    r"DELETE\s+FROM)\s+([`\"\[]?)([A-Za-z_][A-Za-z0-9_]*)\1",
    re.IGNORECASE,
)


class _TrackingConnection(sqlite3.Connection):
    """记录本连接执行过的数据写表名（供事务级同步触发，不依赖方法名）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._written_tables: set[str] = set()

    @staticmethod
    def _collect(sql: str, written: set[str]) -> None:
        for match in _WRITE_SQL_RE.finditer(str(sql)):
            written.add(match.group(2).lower())

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        statement = str(sql or "").lstrip().upper()
        if statement.startswith("ROLLBACK"):
            try:
                return super().execute(sql, parameters)
            finally:
                self._written_tables.clear()
        self._collect(sql, self._written_tables)
        return super().execute(sql, parameters)

    def executemany(
        self,
        sql: str,
        seq_of_parameters: Sequence[Sequence[Any]],
    ) -> Any:
        self._collect(sql, self._written_tables)
        return super().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str) -> Any:
        self._collect(sql_script, self._written_tables)
        return super().executescript(sql_script)

    def rollback(self) -> None:
        try:
            super().rollback()
        finally:
            self._written_tables.clear()


class _ThreadWriteTracker(threading.local):
    def __init__(self) -> None:
        # 每次 _run 调用压入一个集合；连接写入只记录到栈顶，
        # 避免「同名表被前一次调用写过」导致 set 差集误判为空。
        self.stack: list[set[str]] = []


_THREAD_WRITTEN_TABLES = _ThreadWriteTracker()


def _record_written_tables(tables: set[str]) -> None:
    if not tables:
        return
    stack = _THREAD_WRITTEN_TABLES.stack
    if stack:
        stack[-1].update(tables)


class _ManagedConnection:
    """把 sqlite3.Connection 的 with 语义补全为「退出即关闭」。

    sqlite3 连接作为上下文管理器只管理事务、不会关闭连接；全仓
    ``with self._connect() as connection:`` 约 140 处都依赖该写法。
    此前连接要等 GC 才释放句柄，Windows 下临时目录清理（测试/备份）
    会因文件仍被占用抛 PermissionError；这里在退出时显式 commit/rollback
    并 close，行为等价且确定性关闭。

    注意（v1.0-A2 评估记录）：曾尝试按线程复用连接以降低连接创建开销，
    但任何“复用窗口”都会让连接在操作结束后短暂滞留，导致 Windows 下
    临时目录 / 备份文件清理（严格模式）抛 WinError 32——这是 0.9.x
    引入「退出即关闭」的原始原因，属于承重设计。因此保持即开即关，
    性能优化改为：列表聚合用单条 SQL 汇总（见 dashboard_sessions）、
    RuleRuntime 缓存补强，以及提供 execute_read/execute_write 统一访问层。
    """

    __slots__ = ("connection",)

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if exc_type is not None:
                try:
                    self.connection.rollback()
                except sqlite3.Error:
                    pass
            else:
                self.connection.commit()
        finally:
            written = getattr(self.connection, "_written_tables", None)
            _record_written_tables(written or set())
            self.connection.close()
        return False

    def close(self) -> None:
        written = getattr(self.connection, "_written_tables", None)
        _record_written_tables(written or set())
        self.connection.close()

__all__ = [name for name in globals() if not name.startswith("__")]
