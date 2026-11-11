"""Task/message store with per-user SQLite DBs and a global legacy DB."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from application.task_store_persistence import (
    durable_user_db_path,
    flush_persist,
    restore_user_db,
    schedule_persist,
    working_db_path,
    working_user_db_path,
)
from application.utils import sanitize_user_path_segment

logger = logging.getLogger(__name__)

_APPLICATION_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_APPLICATION_DIR, "data")
_GLOBAL_DB_PATH = working_db_path()

DEFAULT_MODEL = "Claude 4.6 Sonnet"
DEFAULT_TASK_TITLE = "New task"
AUTO_TITLE_MAX_LEN = 50
# Upper bound on messages returned for a single task so history that grows over
# time cannot force an unbounded SELECT/fetchall into memory. Callers that need
# older messages can page with the `before`/`limit` parameters.
MAX_MESSAGES_PER_TASK = 2000


class TaskStoreError(Exception):
    """Raised when the task store cannot complete a database operation."""


# Allowlisted ALTER TABLE statements only — never interpolate identifiers into SQL.
_COLUMN_MIGRATIONS: dict[tuple[str, str], str] = {
    ("tasks", "pinned"): "ALTER TABLE tasks ADD COLUMN pinned INTEGER DEFAULT 0",
    ("tasks", "strands_tools_json"): (
        "ALTER TABLE tasks ADD COLUMN strands_tools_json TEXT"
    ),
    ("tasks", "memory_enabled"): (
        "ALTER TABLE tasks ADD COLUMN memory_enabled INTEGER DEFAULT 1"
    ),
}

_USER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  title TEXT,
  runtime_session_id TEXT NOT NULL UNIQUE,
  model_name TEXT,
  skills_json TEXT,
  mcp_servers_json TEXT,
  strands_tools_json TEXT,
  guardrail_enabled INTEGER DEFAULT 0,
  memory_enabled INTEGER DEFAULT 1,
  pinned INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  images_json TEXT,
  tool_events_json TEXT,
  created_at TEXT,
  FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_user_updated
  ON tasks(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_task_created
  ON messages(task_id, created_at ASC);
"""

_GLOBAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  title TEXT,
  runtime_session_id TEXT NOT NULL UNIQUE,
  model_name TEXT,
  skills_json TEXT,
  mcp_servers_json TEXT,
  strands_tools_json TEXT,
  guardrail_enabled INTEGER DEFAULT 0,
  memory_enabled INTEGER DEFAULT 1,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  images_json TEXT,
  tool_events_json TEXT,
  created_at TEXT,
  FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_user_updated
  ON tasks(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_task_created
  ON messages(task_id, created_at ASC);

CREATE TABLE IF NOT EXISTS login_events (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  method TEXT NOT NULL,
  name TEXT,
  picture TEXT,
  logged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_events_logged
  ON login_events(logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_login_events_user
  ON login_events(user_id, logged_at DESC);
"""


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, column_def: str
) -> None:
    """Add a column if missing; re-raise unexpected schema errors."""
    sql = _COLUMN_MIGRATIONS.get((table, column))
    if sql is None or f"ADD COLUMN {column} {column_def}" not in sql:
        raise ValueError(f"Unsupported schema migration: {table}.{column}")
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "duplicate column" in message or "already exists" in message:
            return
        logger.error(
            "Failed to add column %s.%s: %s", table, column, type(exc).__name__
        )
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def _connect_path(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        return _configure_connection(conn)
    except (sqlite3.Error, OSError) as e:
        logger.error("Failed to connect to task store DB: %s", e, exc_info=True)
        raise TaskStoreError("Unable to connect to task store") from e


def _connect_global() -> sqlite3.Connection:
    global _GLOBAL_DB_PATH
    _GLOBAL_DB_PATH = working_db_path()
    return _connect_path(_GLOBAL_DB_PATH)


def _apply_task_column_migrations(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "tasks", "pinned", "INTEGER DEFAULT 0")
    _ensure_column(conn, "tasks", "strands_tools_json", "TEXT")
    _ensure_column(conn, "tasks", "memory_enabled", "INTEGER DEFAULT 1")


def _init_user_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_USER_SCHEMA_SQL)
    _apply_task_column_migrations(conn)


def _init_global_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_GLOBAL_SCHEMA_SQL)
    _apply_task_column_migrations(conn)


def init_db() -> None:
    """Initialize the global DB (login_events + legacy tables)."""
    global _GLOBAL_DB_PATH
    _GLOBAL_DB_PATH = working_db_path()
    with _connect_global() as conn:
        _init_global_schema(conn)


@contextmanager
def _user_db_lock(user_id: str) -> Iterator[None]:
    lock_path = working_user_db_path(user_id) + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _db_ready(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if "." in table:
        schema, name = table.split(".", 1)
        rows = conn.execute(f"PRAGMA {schema}.table_info({name})").fetchall()
    else:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rows}


def _migrate_user_from_legacy(user_db_path: str, user_id: str) -> None:
    """Create user DB and copy this user's tasks/messages from legacy global DB."""
    legacy = working_db_path()
    parent = os.path.dirname(user_db_path)
    os.makedirs(parent, exist_ok=True)
    tmp_path = user_db_path + f".migrating-{os.getpid()}"
    for suffix in ("", "-wal", "-shm"):
        side = tmp_path + suffix
        if os.path.isfile(side):
            os.remove(side)

    task_rows: list[sqlite3.Row] = []
    message_rows: list[sqlite3.Row] = []
    task_cols: list[str] = []
    msg_cols: list[str] = []

    if _db_ready(legacy):
        with _connect_path(legacy) as legacy_conn:
            legacy_task_cols = _table_columns(legacy_conn, "tasks")
            legacy_msg_cols = _table_columns(legacy_conn, "messages")
            task_cols = [
                c
                for c in (
                    "id",
                    "user_id",
                    "title",
                    "runtime_session_id",
                    "model_name",
                    "skills_json",
                    "mcp_servers_json",
                    "strands_tools_json",
                    "guardrail_enabled",
                    "memory_enabled",
                    "pinned",
                    "created_at",
                    "updated_at",
                )
                if c in legacy_task_cols
            ]
            msg_cols = [
                c
                for c in (
                    "id",
                    "task_id",
                    "role",
                    "content",
                    "images_json",
                    "tool_events_json",
                    "created_at",
                )
                if c in legacy_msg_cols
            ]
            if task_cols:
                col_csv = ", ".join(task_cols)
                task_rows = legacy_conn.execute(
                    f"SELECT {col_csv} FROM tasks WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
            if msg_cols and task_rows:
                col_csv = ", ".join(msg_cols)
                message_rows = legacy_conn.execute(
                    f"""
                    SELECT {col_csv}
                    FROM messages
                    WHERE task_id IN (
                      SELECT id FROM tasks WHERE user_id = ?
                    )
                    """,
                    (user_id,),
                ).fetchall()

    conn = _connect_path(tmp_path)
    try:
        _init_user_schema(conn)
        dest_task_cols = _table_columns(conn, "tasks")
        dest_msg_cols = _table_columns(conn, "messages")
        task_cols = [c for c in task_cols if c in dest_task_cols]
        msg_cols = [c for c in msg_cols if c in dest_msg_cols]
        if task_cols and task_rows:
            col_csv = ", ".join(task_cols)
            placeholders = ", ".join("?" for _ in task_cols)
            conn.executemany(
                f"INSERT OR IGNORE INTO tasks ({col_csv}) VALUES ({placeholders})",
                [tuple(row[c] for c in task_cols) for row in task_rows],
            )
        if msg_cols and message_rows:
            col_csv = ", ".join(msg_cols)
            placeholders = ", ".join("?" for _ in msg_cols)
            conn.executemany(
                f"INSERT OR IGNORE INTO messages ({col_csv}) VALUES ({placeholders})",
                [tuple(row[c] for c in msg_cols) for row in message_rows],
            )
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_path, user_db_path)
    for suffix in ("-wal", "-shm"):
        src = tmp_path + suffix
        dst = user_db_path + suffix
        if os.path.isfile(src):
            os.replace(src, dst)
        elif os.path.isfile(dst):
            os.remove(dst)
    logger.info(
        "Migrated user tasks DB for %s -> %s (tasks=%s messages=%s)",
        user_id,
        user_db_path,
        len(task_rows),
        len(message_rows),
    )


def ensure_user_db(user_id: str) -> str:
    """Return working path for the user's tasks/messages DB, creating/migrating if needed."""
    if not sanitize_user_path_segment(user_id):
        raise ValueError(f"Invalid user_id: {user_id!r}")

    working = working_user_db_path(user_id)
    with _user_db_lock(user_id):
        if _db_ready(working):
            with _connect_path(working) as conn:
                _init_user_schema(conn)
            return working

        restored = False
        try:
            restored = restore_user_db(user_id)
        except Exception:
            logger.exception("Failed to restore user DB for %s", user_id)

        if restored and _db_ready(working):
            with _connect_path(working) as conn:
                _init_user_schema(conn)
            return working

        durable = durable_user_db_path(user_id)
        if _db_ready(durable) and not _db_ready(working):
            try:
                restore_user_db(user_id)
            except Exception:
                logger.exception("Failed durable→working restore for %s", user_id)
            if _db_ready(working):
                with _connect_path(working) as conn:
                    _init_user_schema(conn)
                return working

        _migrate_user_from_legacy(working, user_id)
        with _connect_path(working) as conn:
            _init_user_schema(conn)
        flush_persist(user_id)
        return working


def _connect_user(user_id: str) -> sqlite3.Connection:
    path = ensure_user_db(user_id)
    return _connect_path(path)


def _after_write(user_id: str | None = None) -> None:
    schedule_persist(user_id)


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"] or "New task",
        "runtime_session_id": row["runtime_session_id"],
        "model_name": row["model_name"] or DEFAULT_MODEL,
        "skills": json.loads(row["skills_json"] or "[]"),
        "mcp_servers": json.loads(row["mcp_servers_json"] or "[]"),
        "guardrail_enabled": bool(row["guardrail_enabled"]),
        "memory_enabled": bool(row["memory_enabled"]) if "memory_enabled" in row.keys() else True,
        "strands_tools": json.loads(row["strands_tools_json"] or "[]") if "strands_tools_json" in row.keys() else [],
        "pinned": bool(row["pinned"]) if "pinned" in row.keys() else False,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "role": row["role"],
        "content": row["content"] or "",
        "images": json.loads(row["images_json"] or "[]"),
        "tool_events": json.loads(row["tool_events_json"] or "[]"),
        "created_at": row["created_at"],
    }


def list_tasks(user_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with _connect_user(user_id) as conn:
        rows = conn.execute(
            """
            SELECT * FROM tasks
            WHERE user_id = ?
            ORDER BY pinned DESC, updated_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_task(task_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    if not user_id:
        raise ValueError("user_id is required to resolve the per-user task DB")
    with _connect_user(user_id) as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
    return _row_to_task(row) if row else None


def get_task_refreshing(task_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    """Return a task, reloading this user's durable DB once if missing locally."""
    if not user_id:
        return None
    task = get_task(task_id, user_id)
    if task:
        return task
    try:
        restore_user_db(user_id)
    except Exception:
        return None
    if not _db_ready(working_user_db_path(user_id)):
        return None
    return get_task(task_id, user_id)


def create_task(
    user_id: str,
    *,
    model_name: str | None = None,
    skills: list[str] | None = None,
    mcp_servers: list[str] | None = None,
    strands_tools: list[str] | None = None,
    guardrail_enabled: bool = False,
    memory_enabled: bool = True,
    title: str = "New task",
) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    runtime_session_id = str(uuid.uuid4())
    now = _now_iso()
    with _connect_user(user_id) as conn:
        conn.execute(
            """
            INSERT INTO tasks (
              id, user_id, title, runtime_session_id, model_name,
              skills_json, mcp_servers_json, strands_tools_json, guardrail_enabled,
              memory_enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                user_id,
                title,
                runtime_session_id,
                model_name or DEFAULT_MODEL,
                json.dumps(skills or [], ensure_ascii=False),
                json.dumps(mcp_servers or [], ensure_ascii=False),
                json.dumps(strands_tools or [], ensure_ascii=False),
                1 if guardrail_enabled else 0,
                1 if memory_enabled else 0,
                now,
                now,
            ),
        )
    # Flush immediately so sibling ECS tasks / replacements can see the row
    # (debounced persist alone loses creates during rolling deploys).
    flush_persist(user_id)
    return get_task(task_id, user_id)  # type: ignore[return-value]


def _build_update_task_statements(
    task_id: str,
    user_id: str,
    fields: dict[str, Any],
    now: str,
) -> list[tuple[str, tuple[Any, ...]]]:
    """Build parameterized UPDATE statements from validated field values."""
    statements: list[tuple[str, tuple[Any, ...]]] = []

    if "title" in fields and fields["title"] is not None:
        statements.append(
            (
                "UPDATE tasks SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (fields["title"], now, task_id, user_id),
            )
        )
    if "model_name" in fields and fields["model_name"] is not None:
        statements.append(
            (
                "UPDATE tasks SET model_name = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (fields["model_name"], now, task_id, user_id),
            )
        )
    if "guardrail_enabled" in fields and fields["guardrail_enabled"] is not None:
        statements.append(
            (
                "UPDATE tasks SET guardrail_enabled = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (1 if fields["guardrail_enabled"] else 0, now, task_id, user_id),
            )
        )
    if "memory_enabled" in fields and fields["memory_enabled"] is not None:
        statements.append(
            (
                "UPDATE tasks SET memory_enabled = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (1 if fields["memory_enabled"] else 0, now, task_id, user_id),
            )
        )
    if "pinned" in fields and fields["pinned"] is not None:
        statements.append(
            (
                "UPDATE tasks SET pinned = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (1 if fields["pinned"] else 0, now, task_id, user_id),
            )
        )
    if "skills" in fields and fields["skills"] is not None:
        statements.append(
            (
                "UPDATE tasks SET skills_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (json.dumps(fields["skills"], ensure_ascii=False), now, task_id, user_id),
            )
        )
    if "mcp_servers" in fields and fields["mcp_servers"] is not None:
        statements.append(
            (
                "UPDATE tasks SET mcp_servers_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (
                    json.dumps(fields["mcp_servers"], ensure_ascii=False),
                    now,
                    task_id,
                    user_id,
                ),
            )
        )
    if "strands_tools" in fields and fields["strands_tools"] is not None:
        statements.append(
            (
                "UPDATE tasks SET strands_tools_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (
                    json.dumps(fields["strands_tools"], ensure_ascii=False),
                    now,
                    task_id,
                    user_id,
                ),
            )
        )
    return statements


def update_all_tasks(
    user_id: str,
    *,
    model_name: str | None = None,
    guardrail_enabled: bool | None = None,
    memory_enabled: bool | None = None,
) -> int:
    """Apply shared settings to every task for ``user_id``. Returns rows updated."""
    sets: list[str] = []
    values: list[Any] = []
    if model_name is not None:
        sets.append("model_name = ?")
        values.append(str(model_name).strip() or DEFAULT_MODEL)
    if guardrail_enabled is not None:
        sets.append("guardrail_enabled = ?")
        values.append(1 if guardrail_enabled else 0)
    if memory_enabled is not None:
        sets.append("memory_enabled = ?")
        values.append(1 if memory_enabled else 0)
    if not sets:
        return 0
    sets.append("updated_at = ?")
    values.append(_now_iso())
    values.append(user_id)
    with _connect_user(user_id) as conn:
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE user_id = ?",
            values,
        )
        rowcount = cur.rowcount
    # Flush immediately so new-task creates / other instances see Settings.
    flush_persist(user_id)
    return int(rowcount or 0)


def update_all_task_flags(
    user_id: str,
    *,
    guardrail_enabled: bool | None = None,
    memory_enabled: bool | None = None,
) -> int:
    """Backward-compatible alias for flag-only updates."""
    return update_all_tasks(
        user_id,
        guardrail_enabled=guardrail_enabled,
        memory_enabled=memory_enabled,
    )


def update_task(task_id: str, user_id: str, **fields: Any) -> dict[str, Any] | None:
    """Update task fields using static parameterized SQL only (no dynamic SET clauses)."""
    statements = _build_update_task_statements(task_id, user_id, fields, _now_iso())

    if not statements:
        return get_task(task_id, user_id)

    with _connect_user(user_id) as conn:
        for sql, params in statements:
            conn.execute(sql, params)
    _after_write(user_id)
    return get_task(task_id, user_id)


def delete_task(task_id: str, user_id: str) -> bool:
    with _connect_user(user_id) as conn:
        conn.execute("DELETE FROM messages WHERE task_id = ?", (task_id,))
        cur = conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
    if cur.rowcount > 0:
        _after_write(user_id)
    return cur.rowcount > 0


def list_messages(
    task_id: str,
    user_id: str,
    limit: int = MAX_MESSAGES_PER_TASK,
) -> list[dict[str, Any]]:
    """Return messages for a task in chronological order, capped at ``limit``.

    The query is bounded so a long-running task with a large message history
    cannot trigger an unbounded scan. When more than ``limit`` messages exist,
    the most recent ``limit`` are returned (still oldest-first).
    """
    capped = max(1, min(int(limit), MAX_MESSAGES_PER_TASK))
    with _connect_user(user_id) as conn:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE task_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            )
            ORDER BY created_at ASC
            """,
            (task_id, capped),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def derive_task_title_from_message(
    content: str,
    current_title: str | None,
) -> str | None:
    """
    Business rule: auto-generate a title from the first user message
    only while the task still has the default/empty title.
    """
    title = current_title or DEFAULT_TASK_TITLE
    if title not in (DEFAULT_TASK_TITLE, ""):
        return None
    return content.strip()[:AUTO_TITLE_MAX_LEN] or DEFAULT_TASK_TITLE


def _insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    task_id: str,
    role: str,
    content: str,
    images: list[str],
    tool_events: list[dict[str, Any]],
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO messages (
          id, task_id, role, content, images_json, tool_events_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            task_id,
            role,
            content,
            json.dumps(images, ensure_ascii=False),
            json.dumps(tool_events, ensure_ascii=False),
            created_at,
        ),
    )


def _fetch_task_title(conn: sqlite3.Connection, task_id: str) -> str | None:
    row = conn.execute(
        "SELECT title FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return row["title"] if row else None


def _touch_task_after_message(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    updated_at: str,
    title_update: str | None,
) -> None:
    if title_update:
        conn.execute(
            "UPDATE tasks SET updated_at = ?, title = ? WHERE id = ?",
            (updated_at, title_update, task_id),
        )
    else:
        conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            (updated_at, task_id),
        )


def _fetch_message(conn: sqlite3.Connection, message_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    return _row_to_message(row) if row else {}


class MessageService:
    """Coordinates message persistence and title-generation business rules."""

    def add_message(
        self,
        task_id: str,
        role: str,
        content: str,
        *,
        user_id: str,
        images: list[str] | None = None,
        tool_events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        now = _now_iso()
        image_list = images or []
        tool_event_list = tool_events or []

        with _connect_user(user_id) as conn:
            _insert_message(
                conn,
                message_id=message_id,
                task_id=task_id,
                role=role,
                content=content,
                images=image_list,
                tool_events=tool_event_list,
                created_at=now,
            )

            title_update = None
            if role == "user":
                title_update = derive_task_title_from_message(
                    content,
                    _fetch_task_title(conn, task_id),
                )

            _touch_task_after_message(
                conn,
                task_id=task_id,
                updated_at=now,
                title_update=title_update,
            )

        _after_write(user_id)
        with _connect_user(user_id) as conn:
            return _fetch_message(conn, message_id)


def add_message(
    task_id: str,
    role: str,
    content: str,
    *,
    user_id: str,
    images: list[str] | None = None,
    tool_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return MessageService().add_message(
        task_id,
        role,
        content,
        user_id=user_id,
        images=images,
        tool_events=tool_events,
    )
