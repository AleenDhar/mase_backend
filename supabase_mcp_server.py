"""
Supabase MCP Server (Read + Write + DDL)
========================================
Exposes Supabase database tools via the Supabase Management API
(`/v1/projects/{ref}/database/query`).

Access control is delegated to the surrounding transport (e.g. the
DeepAgent /mcp gateway with DISPATCH_SECRET). Tools take SQL only —
no per-call admin token.

Tools (7):
  Read-only:
    - supabase_list_schemas      List all database schemas
    - supabase_list_tables       List all tables in a schema
    - supabase_describe_table    Get columns/types for a specific table
    - supabase_query             Run a SELECT-only SQL query
    - supabase_table_row_count   Approx row counts for all tables in a schema
  Write:
    - supabase_execute           Run DML (INSERT / UPDATE / DELETE / UPSERT / MERGE)
    - supabase_ddl               Run DDL (CREATE / ALTER / DROP / TRUNCATE / GRANT /
                                 REVOKE / RENAME)
"""

import json
import logging
import os
import re

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("supabase_mcp")

PROJECT_REF  = os.environ.get("SUPABASE_PROJECT_REF", "")
ACCESS_TOKEN = os.environ.get("SUPABASE_ACCESS_TOKEN", "")

BASE_URL = f"https://api.supabase.com/v1/projects/{PROJECT_REF}"

mcp = FastMCP("supabase")


def _headers():
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


_AUDIT_SQL_MAX = 4096


def _escape_sql_literal(s) -> str:
    """Escape a Python value for safe inclusion as a SQL string literal."""
    if s is None:
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"


def _affected_rows(result):
    """Best-effort affected row count from the Management API response.
    Returns the length of a non-empty result list, else None."""
    if isinstance(result, list) and len(result) > 0:
        return len(result)
    return None


def _audit(tool: str, sql_text: str, success: bool, affected_rows, chat_id: str, error: str = "") -> None:
    """Append a row to public.agent_audit. Best-effort; logs a warning on
    failure but never raises."""
    try:
        if not ACCESS_TOKEN or not PROJECT_REF:
            logger.warning(
                "agent_audit insert skipped (no Supabase credentials): tool=%s success=%s",
                tool, success,
            )
            return
        truncated = (sql_text or "")[:_AUDIT_SQL_MAX]
        rows_sql = "NULL" if affected_rows is None else str(int(affected_rows))
        audit_sql = (
            "INSERT INTO public.agent_audit "
            "(tool, sql_text, affected_rows, chat_id, success, error) VALUES ("
            f"{_escape_sql_literal(tool)}, "
            f"{_escape_sql_literal(truncated)}, "
            f"{rows_sql}, "
            f"{_escape_sql_literal(chat_id or None)}, "
            f"{'TRUE' if success else 'FALSE'}, "
            f"{_escape_sql_literal(error or None)}"
            ");"
        )
        resp = httpx.post(
            f"{BASE_URL}/database/query",
            headers=_headers(),
            json={"query": audit_sql},
            timeout=10,
        )
        if resp.status_code >= 400:
            logger.warning(
                "agent_audit insert failed: HTTP %s %s | tool=%s success=%s",
                resp.status_code, resp.text[:200], tool, success,
            )
    except Exception as e:
        logger.warning("agent_audit insert raised: %s | tool=%s success=%s", e, tool, success)


def _audited_return(tool: str, sql_text: str, chat_id: str, payload: dict) -> str:
    err = payload.get("error", "") if isinstance(payload, dict) else ""
    _audit(tool, sql_text, False, None, chat_id, err)
    return json.dumps(payload, indent=2, default=str)


def _run_sql(sql: str):
    if not ACCESS_TOKEN or not PROJECT_REF:
        return {"error": "SUPABASE_ACCESS_TOKEN or SUPABASE_PROJECT_REF not configured."}
    try:
        resp = httpx.post(
            f"{BASE_URL}/database/query",
            headers=_headers(),
            json={"query": sql},
            timeout=30,
        )
        if resp.status_code >= 400:
            return {"error": f"Supabase API error {resp.status_code}", "detail": resp.text[:500]}
        return resp.json()
    except httpx.TimeoutException:
        return {"error": "Query timed out after 30 seconds."}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


# SQL classification: route each tool to its own class (read / DML / DDL).
_DML_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "UPSERT", "MERGE", "REPLACE")
_DDL_KEYWORDS = (
    "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE",
    "RENAME", "COMMENT", "REINDEX", "VACUUM", "ANALYZE", "CLUSTER",
    "REFRESH",
)
# Excludes ANALYZE / VACUUM / CLUSTER / REFRESH / REINDEX so legitimate
# `EXPLAIN ANALYZE SELECT ...` is not falsely rejected.
_DESTRUCTIVE_DDL_KEYWORDS = (
    "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE", "RENAME",
)
_READ_KEYWORDS = ("SELECT", "WITH", "EXPLAIN", "SHOW", "VALUES", "TABLE")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _strip_sql(sql: str) -> str:
    """Replace SQL comments and quoted strings with spaces so keyword and
    semicolon scans aren't fooled by content inside literals or comments."""
    out = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            j = sql.find("\n", i)
            if j < 0:
                break
            out.append(" " * (j - i))
            i = j
            continue
        if ch == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            if j < 0:
                break
            out.append(" " * (j + 2 - i))
            i = j + 2
            continue
        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(" " * (j - i))
            i = j
            continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z0-9_]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                if j < 0:
                    break
                end = j + len(tag)
                out.append(" " * (end - i))
                i = end
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _statement_count(stripped: str) -> int:
    parts = [p for p in stripped.split(";") if p.strip()]
    return len(parts)


def _contains_keyword(stripped_upper: str, keywords) -> str:
    for kw in keywords:
        if re.search(r"\b" + kw + r"\b", stripped_upper):
            return kw
    return ""


def _classify_sql(sql: str) -> str:
    """Return 'read' | 'dml' | 'ddl' | 'unknown' based on the leading verb."""
    stripped = _strip_sql(sql).strip()
    if not stripped:
        return "unknown"
    first_word = re.match(r"[A-Za-z]+", stripped)
    if not first_word:
        return "unknown"
    kw = first_word.group(0).upper()
    if kw in _DML_KEYWORDS:
        return "dml"
    if kw in _DDL_KEYWORDS:
        return "ddl"
    if kw in _READ_KEYWORDS:
        return "read"
    return "unknown"


def _enforce_readonly(sql: str):
    """Reject anything that isn't a pure read. Used by `supabase_query`."""
    stripped = _strip_sql(sql)
    if _statement_count(stripped) > 1:
        return "Multiple statements are not allowed in supabase_query. Send one SELECT at a time."
    kind = _classify_sql(sql)
    if kind != "read":
        if kind == "dml":
            return "Write SQL is not allowed in supabase_query. Use supabase_execute."
        if kind == "ddl":
            return "DDL is not allowed in supabase_query. Use supabase_ddl."
        return "Only SELECT, WITH, EXPLAIN, SHOW, VALUES, or TABLE statements are permitted."
    upper = stripped.upper()
    bad = _contains_keyword(upper, _DML_KEYWORDS)
    if bad:
        return f"supabase_query rejects embedded write keyword '{bad}' (data-modifying CTE or EXPLAIN of a write). Use supabase_execute."
    bad = _contains_keyword(upper, _DESTRUCTIVE_DDL_KEYWORDS)
    if bad:
        return f"supabase_query rejects embedded DDL keyword '{bad}'. Use supabase_ddl."
    return None


def _safe_identifier(name: str, kind: str = "identifier"):
    if not name or not _IDENT_RE.match(name):
        return f"Invalid {kind}: must match [A-Za-z_][A-Za-z0-9_]* and be <= 63 chars."
    return None


@mcp.tool()
def supabase_list_schemas() -> str:
    """
    List all schemas in the Supabase database.
    """
    sql = """
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_toast', 'pg_catalog', 'information_schema')
        ORDER BY schema_name;
    """
    return json.dumps(_run_sql(sql), indent=2, default=str)


@mcp.tool()
def supabase_list_tables(schema: str = "public") -> str:
    """
    List all tables in a Supabase schema with approximate row counts and size.

    Args:
        schema: Database schema to list tables from (default: 'public').
    """
    bad = _safe_identifier(schema, "schema")
    if bad:
        return json.dumps({"error": bad})
    sql = f"""
        SELECT
            t.table_name,
            pg_size_pretty(pg_total_relation_size(quote_ident(t.table_name))) AS total_size,
            COALESCE(s.n_live_tup, 0) AS approx_row_count
        FROM information_schema.tables t
        LEFT JOIN pg_stat_user_tables s ON s.relname = t.table_name
        WHERE t.table_schema = '{schema}'
        ORDER BY t.table_name;
    """
    return json.dumps(_run_sql(sql), indent=2, default=str)


@mcp.tool()
def supabase_describe_table(table_name: str, schema: str = "public") -> str:
    """
    Get column names, data types, nullability, and defaults for a specific table.

    Args:
        table_name: Name of the table to describe.
        schema: Schema the table lives in (default: 'public').
    """
    bad = _safe_identifier(schema, "schema") or _safe_identifier(table_name, "table_name")
    if bad:
        return json.dumps({"error": bad})
    sql = f"""
        SELECT
            column_name,
            data_type,
            character_maximum_length,
            is_nullable,
            column_default
        FROM information_schema.columns
        WHERE table_schema = '{schema}' AND table_name = '{table_name}'
        ORDER BY ordinal_position;
    """
    return json.dumps(_run_sql(sql), indent=2, default=str)


@mcp.tool()
def supabase_query(sql: str) -> str:
    """
    Run a read-only SQL SELECT query against the Supabase database.

    Only SELECT, WITH (CTE), EXPLAIN, SHOW, VALUES, or TABLE statements are
    allowed. INSERT / UPDATE / DELETE go to supabase_execute. CREATE / ALTER /
    DROP go to supabase_ddl.

    Args:
        sql: A read-only SQL query.
    """
    write_err = _enforce_readonly(sql)
    if write_err:
        return json.dumps({"error": write_err})
    return json.dumps(_run_sql(sql), indent=2, default=str)


@mcp.tool()
def supabase_table_row_count(schema: str = "public") -> str:
    """
    Get approximate live row counts for all tables in a schema.

    Args:
        schema: Database schema (default: 'public').
    """
    bad = _safe_identifier(schema, "schema")
    if bad:
        return json.dumps({"error": bad})
    sql = f"""
        SELECT
            relname AS table_name,
            n_live_tup AS approx_rows
        FROM pg_stat_user_tables
        WHERE schemaname = '{schema}'
        ORDER BY n_live_tup DESC;
    """
    return json.dumps(_run_sql(sql), indent=2, default=str)


@mcp.tool()
def supabase_execute(sql: str, chat_id: str = "") -> str:
    """
    Run a write SQL statement (DML) against the Supabase database.

    Accepts INSERT, UPDATE, DELETE, UPSERT, MERGE, and REPLACE statements.
    Use a RETURNING clause to get back the affected rows.

    Read-only SQL is rejected — use supabase_query.
    DDL (CREATE / ALTER / DROP / TRUNCATE / GRANT / REVOKE / RENAME) is
    rejected — use supabase_ddl.

    Every call is appended to public.agent_audit (timestamp, tool,
    truncated sql, affected rows, chat_id, success, error).

    Args:
        sql: A write SQL statement (INSERT / UPDATE / DELETE / UPSERT / MERGE).
        chat_id: Optional calling chat id, recorded in the audit log.
    """
    stripped = _strip_sql(sql)
    if _statement_count(stripped) > 1:
        return _audited_return("supabase_execute", sql, chat_id,
            {"error": "Multiple statements are not allowed in supabase_execute. Send one DML at a time."})
    kind = _classify_sql(sql)
    if kind == "read":
        return _audited_return("supabase_execute", sql, chat_id,
            {"error": "supabase_execute is for writes only. Use supabase_query for reads."})
    if kind == "ddl":
        return _audited_return("supabase_execute", sql, chat_id,
            {"error": "supabase_execute does not run DDL. Use supabase_ddl."})
    if kind != "dml":
        return _audited_return("supabase_execute", sql, chat_id,
            {"error": "supabase_execute only accepts DML (INSERT / UPDATE / DELETE / UPSERT / MERGE / REPLACE)."})
    bad = _contains_keyword(stripped.upper(), _DDL_KEYWORDS)
    if bad:
        return _audited_return("supabase_execute", sql, chat_id,
            {"error": f"supabase_execute rejects embedded DDL keyword '{bad}'. Use supabase_ddl."})
    result = _run_sql(sql)
    if isinstance(result, dict) and "error" in result:
        _audit("supabase_execute", sql, False, None, chat_id,
               f"{result.get('error','')} {result.get('detail','')}".strip())
    else:
        _audit("supabase_execute", sql, True, _affected_rows(result), chat_id)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def supabase_ddl(sql: str, chat_id: str = "") -> str:
    """
    Run a DDL (schema-changing) SQL statement against the Supabase database.

    Accepts CREATE, ALTER, DROP, TRUNCATE, GRANT, REVOKE, RENAME, COMMENT,
    REINDEX, VACUUM, ANALYZE, CLUSTER, REFRESH.

    DML (INSERT / UPDATE / DELETE) goes to supabase_execute. Reads go to
    supabase_query.

    Every call is appended to public.agent_audit. The audit table itself
    is created via the one-time migration in
    migrations/0001_agent_audit.sql; supabase_ddl rejects multi-statement
    SQL, so apply each CREATE TABLE / CREATE INDEX statement from that
    file as a separate supabase_ddl call.

    Args:
        sql: A DDL statement.
        chat_id: Optional calling chat id, recorded in the audit log.
    """
    stripped = _strip_sql(sql)
    if _statement_count(stripped) > 1:
        return _audited_return("supabase_ddl", sql, chat_id,
            {"error": "Multiple statements are not allowed in supabase_ddl. Send one DDL at a time."})
    kind = _classify_sql(sql)
    if kind == "read":
        return _audited_return("supabase_ddl", sql, chat_id,
            {"error": "supabase_ddl is for schema changes only. Use supabase_query for reads."})
    if kind == "dml":
        return _audited_return("supabase_ddl", sql, chat_id,
            {"error": "supabase_ddl does not run DML. Use supabase_execute."})
    if kind != "ddl":
        return _audited_return("supabase_ddl", sql, chat_id,
            {"error": "supabase_ddl only accepts DDL (CREATE / ALTER / DROP / TRUNCATE / GRANT / REVOKE / RENAME / COMMENT / REINDEX / VACUUM / ANALYZE / CLUSTER / REFRESH)."})
    result = _run_sql(sql)
    if isinstance(result, dict) and "error" in result:
        _audit("supabase_ddl", sql, False, None, chat_id,
               f"{result.get('error','')} {result.get('detail','')}".strip())
    else:
        _audit("supabase_ddl", sql, True, _affected_rows(result), chat_id)
    return json.dumps(result, indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
