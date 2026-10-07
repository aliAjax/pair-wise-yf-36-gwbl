import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self):
        """Single BEGIN IMMEDIATE transaction; readers inside see its own writes."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS batch_jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    consent_id TEXT NOT NULL,
                    participant_id TEXT,
                    total INTEGER NOT NULL,
                    stored_count INTEGER NOT NULL,
                    failed_count INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_items (
                    batch_id TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sample_id TEXT,
                    error TEXT,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(batch_id, ref)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id, conn=None):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        own = conn is None
        connection = conn or self._connect()
        try:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if own:
                connection.commit()
            result = self._entity_from_row(row)
        finally:
            if own:
                connection.close()
        return result

    def get_entity(self, entity_id, conn=None):
        own = conn is None
        connection = conn or self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        finally:
            if own:
                connection.close()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None, conn=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        own = conn is None
        connection = conn or self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        finally:
            if own:
                connection.close()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value, conn=None):
        return [
            entity
            for entity in self.list_entities(kind=kind, conn=conn)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data, conn=None):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        own = conn is None
        connection = conn or self._connect()
        try:
            if own:
                connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if own:
                connection.commit()
            result = self._entity_from_row(row)
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()
        return result

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail, conn=None):
        own = conn is None
        connection = conn or self._connect()
        try:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )
            if own:
                connection.commit()
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    @staticmethod
    def _job_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "consent_id": row["consent_id"],
            "participant_id": row["participant_id"],
            "total": int(row["total"]),
            "stored_count": int(row["stored_count"]),
            "failed_count": int(row["failed_count"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_batch_job(self, job, conn):
        now = utcnow()
        conn.execute(
            "INSERT INTO batch_jobs(id, kind, status, consent_id, participant_id, total, "
            "stored_count, failed_count, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job["id"], job["kind"], job["status"], job["consent_id"],
                job.get("participant_id"), job["total"], job["stored_count"],
                job["failed_count"], job["created_by"], now, now,
            ),
        )

    def update_batch_job(self, job_id, status, stored_count, failed_count, conn):
        conn.execute(
            "UPDATE batch_jobs SET status = ?, stored_count = ?, failed_count = ?, updated_at = ? "
            "WHERE id = ?",
            (status, stored_count, failed_count, utcnow(), job_id),
        )

    def get_batch_job(self, batch_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM batch_jobs WHERE id = ?", (batch_id,)
            ).fetchone()
        return self._job_from_row(row) if row else None

    def list_batch_items(self, batch_id, status=None, conn=None):
        own = conn is None
        connection = conn or self._connect()
        try:
            sql = "SELECT * FROM batch_items WHERE batch_id = ?"
            params = [batch_id]
            if status:
                sql += " AND status = ?"
                params.append(status)
            sql += " ORDER BY ref"
            rows = connection.execute(sql, params).fetchall()
        finally:
            if own:
                connection.close()
        return [
            {
                "ref": row["ref"],
                "status": row["status"],
                "sample_id": row["sample_id"],
                "error": row["error"],
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def upsert_batch_item(self, batch_id, item, conn):
        conn.execute(
            "INSERT INTO batch_items(batch_id, ref, status, sample_id, error, payload) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(batch_id, ref) DO UPDATE SET "
            "status = excluded.status, sample_id = excluded.sample_id, "
            "error = excluded.error, payload = excluded.payload",
            (
                batch_id,
                item["ref"],
                item["status"],
                item.get("sample_id"),
                item.get("error"),
                json.dumps(item["payload"], ensure_ascii=False, sort_keys=True),
            ),
        )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
