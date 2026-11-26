import base64
from contextlib import closing, contextmanager
from datetime import date, datetime, time
from decimal import Decimal
import json
from math import isfinite
from pathlib import Path
import sqlite3
from time import monotonic
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

MAX_ROWS = 200
CELL_LIMIT = 4096


def row_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_ROWS}")
    return limit


def render(value, depth=0):
    """Bound the size of individual values in tool results."""
    if isinstance(value, str):
        return value[:CELL_LIMIT] + ("…" if len(value) > CELL_LIMIT else "")
    if isinstance(value, bytes):
        return {
            "base64": base64.b64encode(value[:CELL_LIMIT]).decode("ascii"),
            "truncated": len(value) > CELL_LIMIT,
        }
    if isinstance(value, (Decimal, UUID, date, datetime, time)):
        return str(value)
    if depth >= 4 and isinstance(value, (dict, tuple, list)):
        return "…"
    if isinstance(value, dict):
        return {render(str(key)): render(item, depth + 1) for key, item in list(value.items())[:30]}
    if isinstance(value, (tuple, list)):
        return [render(item, depth + 1) for item in value[:30]]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and isfinite(value):
        return value
    return render(str(value))


def result(cursor, limit: int) -> dict:
    rows = []
    truncated = False
    byte_budget = 131_072
    for row in cursor:
        if len(rows) == limit:
            truncated = True
            break
        values = [render(value) for value in row]
        byte_budget -= len(json.dumps(values))
        if byte_budget < 0:
            truncated = True
            break
        rows.append(values)
    return {
        "columns": [column[0] for column in cursor.description],
        "rows": rows,
        "truncated": truncated,
    }


@contextmanager
def sqlite_connection(path: Path):
    if not path.is_file():
        raise ValueError("The configured SQLite file does not exist")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 32_768)
        deadline = monotonic() + 3
        conn.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
        yield conn


def sqlite_schema(path: Path, limit: int):
    with sqlite_connection(path) as conn:
        cursor = conn.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
            "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name LIMIT ?",
            (limit + 1,),
        )
        return result(cursor, limit)


def _read_only_sqlite(action, first, second, database, source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and second and second.lower() != "load_extension":
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def sqlite_query(path: Path, query: str, limit: int):
    with sqlite_connection(path) as conn:
        conn.set_authorizer(_read_only_sqlite)
        cursor = conn.execute(query)
        if cursor.description is None:
            raise ValueError("A read-only SQLite query must return rows")
        return result(cursor, limit)


@contextmanager
def postgres_connection(dsn: str):
    with closing(psycopg.connect(
        dsn, connect_timeout=5,
        options="-c default_transaction_read_only=on -c statement_timeout=5000 "
                "-c lock_timeout=2000 -c search_path=pg_catalog",
        sslmode="verify-full", gssencmode="disable",
    )) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            yield conn


def postgres_schema(dsn: str, schema: str, table: str | None, limit: int):
    with postgres_connection(dsn) as conn:
        cursor = conn.execute(
            """SELECT table_name, ordinal_position, column_name, data_type,
                      is_nullable, column_default
               FROM information_schema.columns
               WHERE table_schema = %s AND (%s::text IS NULL OR table_name = %s)
               ORDER BY table_name, ordinal_position LIMIT %s""",
            (schema, table, table, limit + 1),
        )
        details = {"columns": result(cursor, limit)}
        if table is not None:
            constraints = conn.execute(
                """SELECT k.conname AS name, pg_catalog.pg_get_constraintdef(k.oid) AS definition
                   FROM pg_catalog.pg_constraint k
                   JOIN pg_catalog.pg_class c ON c.oid = k.conrelid
                   JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = %s AND c.relname = %s
                   ORDER BY k.conname LIMIT %s""",
                (schema, table, limit + 1),
            )
            indexes = conn.execute(
                """SELECT indexname AS name, indexdef AS definition
                   FROM pg_catalog.pg_indexes WHERE schemaname = %s AND tablename = %s
                   ORDER BY indexname LIMIT %s""",
                (schema, table, limit + 1),
            )
            details.update(constraints=result(constraints, limit), indexes=result(indexes, limit))
        return details


def postgres_rls(dsn: str, schema: str, table: str, limit: int):
    with postgres_connection(dsn) as conn, conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT current_user AS connected_role, c.relrowsecurity AS rls_enabled,
                      c.relforcerowsecurity AS rls_forced,
                      pg_catalog.row_security_active(c.oid) AS rls_active_for_role,
                      pg_catalog.pg_get_userbyid(c.relowner) AS owner
               FROM pg_catalog.pg_class c
               JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')""",
            (schema, table),
        )
        relation = cursor.fetchone()
        if relation is None:
            raise ValueError("No table with that schema and name")
        cursor.execute(
            """SELECT policyname, permissive, roles, cmd, qual, with_check
               FROM pg_catalog.pg_policies WHERE schemaname = %s AND tablename = %s
               ORDER BY policyname LIMIT %s""",
            (schema, table, limit + 1),
        )
        policies = cursor.fetchall()
        return {
            "table": relation,
            "policies": [render(policy) for policy in policies[:limit]],
            "truncated": len(policies) > limit,
        }


def postgres_sample(dsn: str, schema: str, table: str, columns: list[str], limit: int):
    if not 1 <= len(columns) <= 20 or any(not column or "\0" in column for column in columns):
        raise ValueError("Provide between 1 and 20 valid column names")
    with postgres_connection(dsn) as conn:
        relation = conn.execute(
            """SELECT 1 FROM pg_catalog.pg_class c
               JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')
               AND n.nspname NOT IN ('pg_catalog', 'information_schema')
               AND n.nspname NOT LIKE 'pg_toast%%'""",
            (schema, table),
        )
        if relation.fetchone() is None:
            raise ValueError("No application table with that schema and name")
        query = sql.SQL("SELECT {} FROM {} LIMIT %s").format(
            sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.Identifier(schema, table),
        )
        return result(conn.execute(query, (limit + 1,)), limit)
