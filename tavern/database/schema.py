"""Atomic Schema 29 bootstrap assembled from bounded catalog modules."""

from .common import *
from .schema_catalog import SCHEMA_SQL as CATALOG_SCHEMA_SQL
from .schema_identity import (
    POST_OPERATIONS_SQL as IDENTITY_GUARD_SQL,
    SCHEMA_SQL as IDENTITY_SCHEMA_SQL,
)
from .schema_operations import SCHEMA_SQL as OPERATIONS_SCHEMA_SQL
from .schema_analysis import SCHEMA_SQL as ANALYSIS_SCHEMA_SQL
from .schema_extensions import SCHEMA_SQL as EXTENSIONS_SCHEMA_SQL
from .schema_rc10 import (
    TABLE_SQL as RC10_TABLE_SQL,
    TRIGGER_STATEMENTS as RC10_TRIGGER_STATEMENTS,
)


class SchemaMixin:
    def _initialize(self) -> None:
        """Create the full catalog in one explicit SQLite transaction.

        The schema text is split only for source ownership and file-size policy.
        SQLite receives one ordered script beginning with ``BEGIN IMMEDIATE``;
        the schema marker, generation columns, and initial world revisions are
        committed together. A failure rolls the whole initialization back.
        """

        schema_marker_sql = f"""
        INSERT INTO tavern_meta(key, value)
        VALUES ('schema_version', '{int(DATABASE_SCHEMA_VERSION)}')
        ON CONFLICT(key) DO UPDATE SET value=excluded.value;
        """
        schema_sql = "\n".join(
            (
                "BEGIN IMMEDIATE;",
                CATALOG_SCHEMA_SQL,
                IDENTITY_SCHEMA_SQL,
                OPERATIONS_SCHEMA_SQL,
                IDENTITY_GUARD_SQL,
                ANALYSIS_SCHEMA_SQL,
                EXTENSIONS_SCHEMA_SQL,
                RC10_TABLE_SQL,
                schema_marker_sql,
            )
        )
        with self._schema_lock:
            with self._connect() as connection:
                try:
                    connection.executescript(schema_sql)
                    self._ensure_storage_outbox_generations(connection)
                    self._sanitize_configuration_revisions(connection)
                    self._backfill_world_revisions(connection)
                    for statement in RC10_TRIGGER_STATEMENTS:
                        connection.execute(statement)
                    connection.execute("COMMIT")
                except Exception:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
