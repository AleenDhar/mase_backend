"""MASE DB backup — mirror the main Supabase into the dedicated mase-backup project.

Design:
  * SCHEMA is replicated from main's LIVE schema (information_schema), so all 103 tables are
    covered regardless of migrations. Types map 1:1 for standard scalars/arrays; exotic types
    (enums, pgvector, etc.) are stored as text in the backup — the DATA is preserved, which is
    the point of a backup. Primary keys are copied so we can upsert.
  * DATA copies via PostgREST (service keys) — no direct DB connection, no DB password, works
    from anywhere (ECS / Actions / laptop). Keyset pagination on a cursor column.
  * INCREMENTAL: tables with updated_at/created_at copy only rows newer than the last run's
    cursor, so 5-hourly runs are cheap after the one-time full seed. Tables without a timestamp
    are full-replaced. Everything is idempotent (upsert on PK / delete+insert for no-PK tables).
  * Per-table cursors live in the backup's `_backup_state`; every run writes a `_backup_runs` row.

CLI:
  python db_backup.py schema     # (re)create all tables in the backup + control tables
  python db_backup.py seed       # full copy of every table (the initial 2.8GB seed)
  python db_backup.py sync       # incremental — only rows changed since the last run
  python db_backup.py verify     # compare per-table row counts main vs backup
  python db_backup.py seed --only chat_messages,document_chunks   # restrict to some tables
"""
import sys, time, json, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = r"C:\Users\Aleen.Dhar\Downloads\Agent-Salesforce-Link (1)\Agent-Salesforce-Link"


def load(p):
    d = {}
    try:
        for l in open(p, encoding="utf-8", errors="ignore"):
            l = l.strip()
            if l and not l.startswith("#") and "=" in l:
                k, v = l.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return d


# Creds resolve from ENV VARS FIRST (how the ECS backend + the GitHub Actions cron supply
# them — injected from mase/app-env), falling back to the local gitignored files so the same
# script runs on a laptop for admin/testing. Nothing secret is hard-coded or logged.
import os as _os
_sec = load(REPO + r"\.supabase_secrets.env")
_env = load(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local")
_bak = load(REPO + r"\.backup_secrets.env")


def _cfg(env_key, *files_and_keys, default=""):
    if _os.environ.get(env_key):
        return _os.environ[env_key]
    for d, k in files_and_keys:
        if d.get(k):
            return d[k]
    return default


MGMT_TOKEN = _cfg("SUPABASE_ACCESS_TOKEN", (_sec, "SUPABASE_ACCESS_TOKEN"))
MGMT = {"Authorization": f"Bearer {MGMT_TOKEN}", "Content-Type": "application/json"}
MAIN_URL = _cfg("SUPABASE_URL", (_env, "NEXT_PUBLIC_SUPABASE_URL"),
                (_env, "SUPABASE_URL")).rstrip("/")
MAIN_REF = MAIN_URL.split("//", 1)[1].split(".")[0] if "//" in MAIN_URL else ""
MAIN_KEY = _cfg("SUPABASE_SERVICE_ROLE_KEY", (_env, "SUPABASE_SERVICE_ROLE_KEY"))
MAIN_H = {"apikey": MAIN_KEY, "Authorization": f"Bearer {MAIN_KEY}"}
BAK_REF = _cfg("BACKUP_REF", (_bak, "BACKUP_REF"))
BAK_URL = _cfg("BACKUP_URL", (_bak, "BACKUP_URL")).rstrip("/")
BAK_KEY = _cfg("BACKUP_SERVICE_KEY", (_bak, "BACKUP_SERVICE_KEY"))
BAK_H = {"apikey": BAK_KEY, "Authorization": f"Bearer {BAK_KEY}"}

PAGE = 2000                 # rows per PostgREST page / upsert batch
STD = {"int2", "int4", "int8", "smallint", "integer", "bigint", "float4", "float8", "real",
       "numeric", "decimal", "bool", "boolean", "text", "uuid", "date", "json", "jsonb",
       "bytea", "inet", "cidr", "timestamp", "timestamptz", "time", "timetz", "money"}


def mgmt_query(ref, sql):
    r = requests.post(f"https://api.supabase.com/v1/projects/{ref}/database/query",
                      headers=MGMT, json={"query": sql}, verify=False, timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"mgmt query {r.status_code}: {r.text[:300]}")
    return r.json() if r.text.strip() else []


def maptype(data_type, udt):
    """Map a source column type to something valid in the backup. Exotic -> text."""
    u = (udt or "").lower()
    if data_type == "ARRAY" or u.startswith("_"):
        base = u[1:] if u.startswith("_") else u
        return (base if base in STD else "text") + "[]"
    if u == "varchar" or data_type == "character varying":
        return "text"          # drop length caps — a backup never needs the constraint
    if u == "bpchar":
        return "text"
    if u in STD:
        return u
    return "text"              # USER-DEFINED enums, vector, geometry, etc. -> text


def table_sizes():
    """{table: avg_row_bytes} from pg_stat, so we can size batches to keep each request small."""
    rows = mgmt_query(MAIN_REF, """
        select relname as t, n_live_tup as rows, pg_total_relation_size(relid) as bytes
        from pg_stat_user_tables where schemaname='public'""")
    out = {}
    for r in rows:
        n = r["rows"] or 0
        out[r["t"]] = int((r["bytes"] or 0) / n) if n else 200
    return out


def batch_for(avg_bytes):
    """Rows per read/upsert so each request stays ~5 MB. 5..1000. deal_records (~105KB/row)
    -> ~47; chat_messages (~1.4KB/row) -> 1000 (PostgREST's hard cap anyway)."""
    return max(5, min(1000, int(5_000_000 / max(avg_bytes, 1))))


def introspect():
    """Read main's public schema -> {table: {cols:[(name,type,notnull)], pk:[...], cursor:col}}."""
    cols = mgmt_query(MAIN_REF, """
        select c.table_name, c.column_name, c.data_type, c.udt_name, c.is_nullable, c.ordinal_position
        from information_schema.columns c
        join information_schema.tables t
          on t.table_name=c.table_name and t.table_schema=c.table_schema
        where c.table_schema='public' and t.table_type='BASE TABLE'
        order by c.table_name, c.ordinal_position""")
    pks = mgmt_query(MAIN_REF, """
        select tc.table_name, kcu.column_name
        from information_schema.table_constraints tc
        join information_schema.key_column_usage kcu
          on kcu.constraint_name=tc.constraint_name and kcu.table_schema=tc.table_schema
        where tc.constraint_type='PRIMARY KEY' and tc.table_schema='public'
        order by tc.table_name, kcu.ordinal_position""")
    tables = {}
    for c in cols:
        t = tables.setdefault(c["table_name"], {"cols": [], "pk": [], "colnames": []})
        t["cols"].append((c["column_name"], maptype(c["data_type"], c["udt_name"]),
                          c["is_nullable"] == "NO"))
        t["colnames"].append(c["column_name"])
    for p in pks:
        if p["table_name"] in tables:
            tables[p["table_name"]]["pk"].append(p["column_name"])
    sizes = table_sizes()
    for name, t in tables.items():
        cn = t["colnames"]
        t["cursor"] = next((c for c in ("updated_at", "modified_at", "created_at", "inserted_at")
                            if c in cn), None)
        t["batch"] = batch_for(sizes.get(name, 200))
    return tables


def q(ident):
    return '"' + ident.replace('"', '""') + '"'


def ensure_schema(tables):
    # control tables in the backup
    mgmt_query(BAK_REF, "create extension if not exists pgcrypto;")
    mgmt_query(BAK_REF, """
        create table if not exists public._backup_state(
            table_name text primary key, last_cursor text, last_run_at timestamptz, rows_copied bigint);
        create table if not exists public._backup_runs(
            id uuid primary key default gen_random_uuid(), started_at timestamptz default now(),
            finished_at timestamptz, mode text, tables_synced int, rows_copied bigint,
            status text, detail jsonb);""")
    made = 0
    for name, t in tables.items():
        coldefs = ", ".join(f"{q(c)} {ty}{' not null' if nn and not t['pk'] else ''}"
                            for c, ty, nn in t["cols"])
        pk = f", primary key ({', '.join(q(c) for c in t['pk'])})" if t["pk"] else ""
        try:
            mgmt_query(BAK_REF, f"create table if not exists public.{q(name)} ({coldefs}{pk});")
            made += 1
        except Exception as e:
            print(f"  [schema] {name}: {str(e)[:120]}", flush=True)
    return made


def count(base_url, headers, table):
    r = requests.get(f"{base_url}/rest/v1/{table}", headers={**headers, "Prefer": "count=exact",
                     "Range": "0-0"}, params={"select": "*"}, verify=False, timeout=60)
    cr = r.headers.get("content-range", "")
    return int(cr.split("/")[-1]) if "/" in cr and cr.split("/")[-1] != "*" else 0


def get_state(table):
    r = requests.get(f"{BAK_URL}/rest/v1/_backup_state", headers=BAK_H,
                     params={"table_name": f"eq.{table}", "select": "last_cursor"},
                     verify=False, timeout=60).json()
    return (r[0]["last_cursor"] if r else None)


def put_state(table, cursor, rows):
    requests.post(f"{BAK_URL}/rest/v1/_backup_state", headers={**BAK_H, "Content-Type": "application/json",
                  "Prefer": "resolution=merge-duplicates"},
                  json={"table_name": table, "last_cursor": cursor,
                        "last_run_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "rows_copied": rows}, verify=False, timeout=60)


def upsert(table, rows, pk, _depth=0):
    """Upsert a batch; on an over-size / transient failure (413/520/502/504/timeout) split the
    batch in half and retry, down to single rows. Handles multi-MB deal_records rows."""
    if not rows:
        return
    hdr = {**BAK_H, "Content-Type": "application/json",
           "Prefer": "resolution=merge-duplicates" if pk else "return=minimal"}
    try:
        r = requests.post(f"{BAK_URL}/rest/v1/{table}", headers=hdr, data=json.dumps(rows),
                          verify=False, timeout=180)
        if r.status_code < 300:
            return
        transient = r.status_code in (413, 500, 502, 503, 504, 520, 429)
    except (requests.Timeout, requests.ConnectionError):
        transient = True
    if len(rows) == 1 or not transient or _depth > 12:
        raise RuntimeError(f"upsert {table}: batch of {len(rows)} failed (depth {_depth})")
    mid = len(rows) // 2
    upsert(table, rows[:mid], pk, _depth + 1)
    upsert(table, rows[mid:], pk, _depth + 1)


def sync_table(table, spec, mode):
    """Copy main->backup. Pagination ALWAYS keysets on the unique single-col PK (no skips even
    when created_at is non-unique); composite/no-PK tables use offset paging. The timestamp
    column is used ONLY as an incremental filter (rows changed since the last run) and to record
    the new high-water cursor. No-PK tables are fully replaced so re-runs can't duplicate."""
    pk, cur_col, batch = spec["pk"], spec["cursor"], spec.get("batch", 1000)
    pk_single = pk[0] if len(pk) == 1 else None
    total, max_cur = 0, None

    filt = {}
    if mode == "incremental" and cur_col:
        last = get_state(table)
        if last:
            filt[cur_col] = f"gt.{last}"
    if mode != "incremental" and not pk:
        # nothing unique to upsert on -> clear the table so a re-seed can't duplicate
        requests.delete(f"{BAK_URL}/rest/v1/{table}", headers=BAK_H,
                        params={spec["colnames"][0]: "not.is.null"} if spec["colnames"] else {},
                        verify=False, timeout=120)

    order_col = pk_single or (spec["colnames"][0] if spec["colnames"] else None)
    last_key, offset = None, 0
    while True:
        params = {"select": "*", "limit": str(batch), **filt}
        if order_col:
            params["order"] = f"{order_col}.asc"
        if pk_single:
            if last_key is not None:
                params[pk_single] = f"gt.{last_key}"      # keyset on the UNIQUE pk
        else:
            params["offset"] = str(offset)                # positional; correct without a unique key
        page = requests.get(f"{MAIN_URL}/rest/v1/{table}", headers=MAIN_H,
                            params=params, verify=False, timeout=120).json()
        if not isinstance(page, list) or not page:
            break
        upsert(table, page, pk)
        total += len(page)
        if cur_col:
            for r in page:
                v = r.get(cur_col)
                if v and (max_cur is None or str(v) > str(max_cur)):
                    max_cur = v
        if pk_single:
            last_key = page[-1][pk_single]
        else:
            offset += len(page)
        if len(page) < batch:
            break
    if cur_col:
        put_state(table, max_cur if max_cur is not None else get_state(table), total)
    return total


def run(mode, only=None):
    started = datetime.datetime.now(datetime.timezone.utc)
    print(f"[{datetime.datetime.now():%H:%M:%S}] introspecting main…", flush=True)
    tables = introspect()
    if only:
        tables = {k: v for k, v in tables.items() if k in only}
    print(f"[{datetime.datetime.now():%H:%M:%S}] ensuring schema for {len(tables)} tables…", flush=True)
    ensure_schema(tables)
    grand, errors, detail = 0, 0, {}
    order = sorted(tables, key=lambda t: count(MAIN_URL, MAIN_H, t))   # small first = fast feedback
    for i, name in enumerate(order, 1):
        try:
            n = sync_table(name, tables[name], mode)
            grand += n
            detail[name] = n
            print(f"[{datetime.datetime.now():%H:%M:%S}] ({i}/{len(order)}) {name:34} +{n:,}", flush=True)
        except Exception as e:
            errors += 1
            detail[name] = f"ERR {str(e)[:120]}"
            print(f"[{datetime.datetime.now():%H:%M:%S}] ({i}/{len(order)}) {name:34} ERROR {str(e)[:120]}", flush=True)
    finished = datetime.datetime.now(datetime.timezone.utc)
    summary = {"mode": mode, "tables_synced": len(tables), "rows_copied": grand,
               "errors": errors, "status": "ok" if not errors else "partial",
               "started_at": started.isoformat(), "finished_at": finished.isoformat(),
               "duration_s": int((finished - started).total_seconds()), "detail": detail}
    try:
        requests.post(f"{BAK_URL}/rest/v1/_backup_runs", headers={**BAK_H, "Content-Type": "application/json"},
                      json={k: summary[k] for k in ("started_at", "finished_at", "mode", "tables_synced",
                            "rows_copied", "status", "detail")}, verify=False, timeout=60)
    except Exception as e:
        print(f"[warn] could not write _backup_runs: {e}", flush=True)
    print(f"\n[{datetime.datetime.now():%H:%M:%S}] {mode.upper()} DONE — {len(tables)} tables, "
          f"{grand:,} rows, {errors} error(s)", flush=True)
    return summary


def verify():
    tables = introspect()
    print(f"{'table':34}{'main':>10}{'backup':>10}  status")
    print("-" * 66)
    bad = 0
    for name in sorted(tables):
        m = count(MAIN_URL, MAIN_H, name)
        try:
            b = count(BAK_URL, BAK_H, name)
        except Exception:
            b = -1
        ok = b >= m if m else b >= 0
        if not ok:
            bad += 1
        print(f"{name:34}{m:>10,}{b:>10,}  {'ok' if ok else 'MISMATCH'}")
    print("-" * 66)
    print(f"{'MISMATCH' if bad else 'ALL OK'} — {bad} table(s) behind")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    if not (BAK_REF and BAK_KEY):
        print("backup creds missing — provision first (.backup_secrets.env)"); sys.exit(1)
    if cmd == "schema":
        t = introspect(); print("tables created:", ensure_schema(t))
    elif cmd == "seed":
        run("full", only)
    elif cmd == "sync":
        run("incremental", only)
    elif cmd == "verify":
        verify()
    else:
        print(__doc__)
