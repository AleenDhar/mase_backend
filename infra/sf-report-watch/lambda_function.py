"""
mase-sf-report-watch — scheduled Salesforce-report → VIBE-project dispatcher.

Runs on an EventBridge schedule (every 5 min). Watches the object behind the
Salesforce report "APAC GTM MQL Global_V1" (00OP7000005v4TsMAI) — which is a
"Contacts with MQL History" matrix report filtered to:

    MQL_History__c.MQL__c = true  AND  Contact.Account.Geography__c = 'APAC'

For each NEW MQL_History__c row (one that appeared since the last poll) it POSTs
to the VIBE app to kick a project run under the contact's owning BDR:

    POST {VIBE_DISPATCH_URL}   (default .../api/workflows/dispatch-abm)
    header Authorization: Bearer {DISPATCH_SECRET}
    body {bdr_email, bdr_name, account_id, account_name, project_id, message, model}

"New" is detected with a high-water mark on Salesforce CreatedDate (second
precision — MQL_Date_Time__c is rounded to 5-min buckets so it would drop ties),
PLUS a dedup ledger keyed on the MQL_History__c record id, so every row is
dispatched exactly once even across overlapping windows. State lives in Supabase
(sf_report_watch_cursor + sf_report_watch_log — see migrations/0013).

First run seeds the watermark to "now" (unless SEED_WATERMARK_ISO is set), so an
initial deploy does NOT fire the existing backlog. MAX_DISPATCH_PER_RUN caps how
many dispatches a single invocation may fire (backlog drains over subsequent
runs). Set DRY_RUN=true to log candidates without dispatching.

Zero third-party deps — runs on the stock python3.12 Lambda runtime (mirrors
sf-cdc-bridge). Salesforce auth is the SOAP username-password login (same creds
the backend's simple_salesforce uses: SF_USERNAME / SF_PASSWORD /
SF_SECURITY_TOKEN / SF_DOMAIN); the returned session id is used as the REST
Bearer token.
"""
import json
import os
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

# ── Salesforce ────────────────────────────────────────────────────────────────
SF_USERNAME = os.environ["SF_USERNAME"]
SF_PASSWORD = os.environ["SF_PASSWORD"]
SF_SECURITY_TOKEN = os.environ.get("SF_SECURITY_TOKEN", "")
SF_DOMAIN = os.environ.get("SF_DOMAIN", "login")          # 'login' | 'test' | my-domain host
SF_API_VERSION = os.environ.get("SF_API_VERSION", "59.0")

# ── Supabase (poller state) ───────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]                 # service-role key

# ── VIBE dispatch ─────────────────────────────────────────────────────────────
VIBE_DISPATCH_URL = os.environ.get(
    "VIBE_DISPATCH_URL", "https://zycus-deal.vercel.app/api/workflows/dispatch-abm")
DISPATCH_SECRET = os.environ.get("DISPATCH_SECRET", "")
MQL_ABM_PROJECT_ID = os.environ.get("MQL_ABM_PROJECT_ID", "")   # target VIBE project UUID
DISPATCH_MODEL = os.environ.get("MODEL", "anthropic:claude-sonnet-4-20250514")
FALLBACK_BDR_EMAIL = os.environ.get("FALLBACK_BDR_EMAIL", "").strip()

# ── Watch config ──────────────────────────────────────────────────────────────
REPORT_ID = os.environ.get("REPORT_ID", "00OP7000005v4TsMAI")   # state key + label
GEOGRAPHY = os.environ.get("GEOGRAPHY", "APAC")
MAX_DISPATCH_PER_RUN = int(os.environ.get("MAX_DISPATCH_PER_RUN", "25"))
SEED_WATERMARK_ISO = os.environ.get("SEED_WATERMARK_ISO", "").strip()  # optional backfill start
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")

HTTP_TIMEOUT = 30


# ── Salesforce SOAP login → (instance_url, session_id) ────────────────────────
def _sf_login_host():
    d = SF_DOMAIN.strip()
    if d in ("login", "test"):
        return "https://%s.salesforce.com" % d
    # a full my-domain host was supplied
    return "https://%s" % d.replace("https://", "").replace("http://", "").rstrip("/")


def sf_login():
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:urn="urn:partner.soap.sforce.com"><env:Body><urn:login>'
        '<urn:username>%s</urn:username><urn:password>%s</urn:password>'
        '</urn:login></env:Body></env:Envelope>'
    ) % (xml_escape(SF_USERNAME), xml_escape(SF_PASSWORD + SF_SECURITY_TOKEN))
    url = "%s/services/Soap/u/%s" % (_sf_login_host(), SF_API_VERSION)
    req = urllib.request.Request(
        url, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "login"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    session_id = _first_local(root, "sessionId")
    server_url = _first_local(root, "serverUrl")
    if not session_id or not server_url:
        raise RuntimeError("SF login failed; no sessionId/serverUrl in response")
    parts = urllib.parse.urlparse(server_url)
    instance_url = "%s://%s" % (parts.scheme, parts.netloc)
    return instance_url, session_id


def _first_local(root, local_name):
    """Return text of the first element whose tag local-name matches (ns-agnostic)."""
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == local_name and el.text:
            return el.text.strip()
    return None


def sf_query(instance_url, session_id, soql):
    """Run a SOQL query via REST, following pagination. Returns list of records."""
    records = []
    path = "/services/data/v%s/query?q=%s" % (SF_API_VERSION, urllib.parse.quote(soql))
    url = instance_url + path
    while url:
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + session_id,
                          "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read() or b"{}")
        records.extend(data.get("records", []))
        nxt = data.get("nextRecordsUrl")
        url = instance_url + nxt if nxt else None
    return records


# ── Supabase REST helpers ─────────────────────────────────────────────────────
def _sb_headers(extra=None):
    h = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY,
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _sb_request(method, path, body=None, headers=None):
    url = SUPABASE_URL + "/rest/v1/" + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_sb_headers(headers))
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        print("[supabase] %s %s -> HTTP %s: %s" % (method, path, e.code, e.read()[:300]))
        raise


def get_watermark():
    """Read the high-water mark; seed it (start-from-now, or SEED_WATERMARK_ISO) if absent."""
    rows = _sb_request(
        "GET", "sf_report_watch_cursor?report_id=eq.%s&select=watermark"
        % urllib.parse.quote(REPORT_ID))
    if rows:
        return rows[0]["watermark"]
    seed = SEED_WATERMARK_ISO or _now_iso()
    _sb_request("POST", "sf_report_watch_cursor",
                body={"report_id": REPORT_ID, "watermark": seed, "updated_at": _now_iso()},
                headers={"Prefer": "resolution=merge-duplicates"})
    print("[cursor] seeded watermark for %s = %s" % (REPORT_ID, seed))
    return seed


def set_watermark(watermark):
    _sb_request("POST", "sf_report_watch_cursor",
                body={"report_id": REPORT_ID, "watermark": watermark, "updated_at": _now_iso()},
                headers={"Prefer": "resolution=merge-duplicates"})


def already_seen(mqlh_ids):
    """Return the subset of ids already present in the dedup ledger."""
    if not mqlh_ids:
        return set()
    # SF ids are safe alnum, so a bare comma list is fine for PostgREST in.()
    in_list = ",".join(mqlh_ids)
    rows = _sb_request("GET", "sf_report_watch_log?mqlh_id=in.(%s)&select=mqlh_id"
                       % urllib.parse.quote(in_list))
    return {r["mqlh_id"] for r in rows}


def log_dispatch(row, status, chat_id=None, error=None):
    c = row.get("Contact__r") or {}
    acct = (c.get("Account") or {})
    owner = (c.get("Owner") or {})
    _sb_request("POST", "sf_report_watch_log", body={
        "mqlh_id": row["Id"],
        "report_id": REPORT_ID,
        "contact_id": c.get("Id"),
        "contact_name": c.get("Name"),
        "account_id": c.get("AccountId"),
        "account_name": acct.get("Name"),
        "bdr_email": owner.get("Email"),
        "bdr_name": owner.get("Name"),
        "campaign_type": row.get("Campaign_Type__c"),
        "mql_status": row.get("MQL_Status__c"),
        "mql_score": row.get("MQL_Score__c"),
        "mql_date_time": row.get("MQL_Date_Time__c"),
        "created_date": row.get("CreatedDate"),
        "chat_id": chat_id,
        "status": status,
        "error": error,
        "dispatched_at": _now_iso(),
    }, headers={"Prefer": "resolution=merge-duplicates"})


# ── Dispatch to VIBE ──────────────────────────────────────────────────────────
def build_message(row):
    c = row.get("Contact__r") or {}
    acct = (c.get("Account") or {})
    owner = (c.get("Owner") or {})
    return (
        'New APAC MQL from report "APAC GTM MQL Global_V1".\n\n'
        "MQL History ID: %s\n"
        "MQL Date/Time:  %s\n"
        "Campaign Type:  %s\n"
        "MQL Status:     %s  (Score: %s)\n\n"
        "Contact:        %s — %s\n"
        "Email:          %s\n"
        "Contact ID:     %s\n\n"
        "Account:        %s  (%s)\n"
        "Account ID:     %s\n\n"
        "Owner (BDR):    %s — %s\n\n"
        "Task: Run MQL intake for this contact."
    ) % (
        row.get("Id"), row.get("MQL_Date_Time__c"), row.get("Campaign_Type__c"),
        row.get("MQL_Status__c"), row.get("MQL_Score__c"),
        c.get("Name"), c.get("Title"), c.get("Email"), c.get("Id"),
        acct.get("Name"), acct.get("Geography__c"), c.get("AccountId"),
        owner.get("Name"), owner.get("Email"),
    )


def dispatch(row):
    """POST one MQL to VIBE. Returns (status, chat_id, error)."""
    c = row.get("Contact__r") or {}
    acct = (c.get("Account") or {})
    owner = (c.get("Owner") or {})
    bdr_email = (owner.get("Email") or "").strip()
    if not bdr_email:
        bdr_email = FALLBACK_BDR_EMAIL
    if not bdr_email:
        return "skipped_no_bdr", None, "no owner email and no FALLBACK_BDR_EMAIL"

    payload = {
        "bdr_email": bdr_email,
        "bdr_name": owner.get("Name") or bdr_email,
        "account_id": c.get("AccountId"),
        "account_name": acct.get("Name"),
        "project_id": MQL_ABM_PROJECT_ID or None,
        "message": build_message(row),
        "model": DISPATCH_MODEL,
    }
    if DRY_RUN:
        print("[dry_run] would dispatch %s (%s / %s)"
              % (row["Id"], acct.get("Name"), bdr_email))
        return "dry_run", None, None

    req = urllib.request.Request(
        VIBE_DISPATCH_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + DISPATCH_SECRET})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read() or b"{}")
        return "dispatched", data.get("chat_id"), None
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        # Retry once under the fallback BDR if the owner isn't a VIBE user.
        if e.code == 404 and FALLBACK_BDR_EMAIL and bdr_email != FALLBACK_BDR_EMAIL:
            payload["bdr_email"] = FALLBACK_BDR_EMAIL
            payload["bdr_name"] = FALLBACK_BDR_EMAIL
            try:
                req2 = urllib.request.Request(
                    VIBE_DISPATCH_URL, data=json.dumps(payload).encode("utf-8"),
                    method="POST", headers={"Content-Type": "application/json",
                                            "Authorization": "Bearer " + DISPATCH_SECRET})
                with urllib.request.urlopen(req2, timeout=HTTP_TIMEOUT) as resp2:
                    data = json.loads(resp2.read() or b"{}")
                return "dispatched", data.get("chat_id"), "fallback_bdr"
            except Exception as e2:  # noqa: BLE001
                return "failed", None, "fallback failed: %s" % e2
        return "failed", None, "HTTP %s: %s" % (e.code, detail)
    except Exception as e:  # noqa: BLE001
        return "failed", None, "%s: %s" % (type(e).__name__, e)


# ── main ──────────────────────────────────────────────────────────────────────
def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_sf_datetime(iso):
    """Normalize any ISO8601 (Supabase '+00:00', SF '.000+0000', or 'Z', with or
    without fractional seconds) to a SOQL dateTime literal 'YYYY-MM-DDThh:mm:ssZ'."""
    s = iso.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_soql(watermark_iso):
    return (
        "SELECT Id, MQL_Date_Time__c, Campaign_Type__c, MQL_Status__c, MQL_Score__c, CreatedDate, "
        "Contact__r.Id, Contact__r.Name, Contact__r.Email, Contact__r.Title, "
        "Contact__r.AccountId, Contact__r.Account.Name, Contact__r.Account.Geography__c, "
        "Contact__r.Owner.Name, Contact__r.Owner.Email "
        "FROM MQL_History__c "
        "WHERE MQL__c = true "
        "AND Contact__r.Account.Geography__c = '%s' "
        "AND CreatedDate > %s "
        "ORDER BY CreatedDate ASC "
        "LIMIT 200"
    ) % (GEOGRAPHY.replace("'", "\\'"), _to_sf_datetime(watermark_iso))


def handler(event, context):
    watermark = get_watermark()
    soql = build_soql(watermark)
    instance_url, session_id = sf_login()
    candidates = sf_query(instance_url, session_id, soql)
    print("[poll] report=%s watermark=%s candidates=%d dry_run=%s"
          % (REPORT_ID, watermark, len(candidates), DRY_RUN))

    if not candidates:
        return {"candidates": 0, "dispatched": 0, "watermark": watermark}

    seen = already_seen([r["Id"] for r in candidates])

    dispatched, skipped, failed = [], [], []
    new_watermark = watermark
    processed = 0
    for row in candidates:                      # sorted by CreatedDate ASC
        if processed >= MAX_DISPATCH_PER_RUN:
            print("[cap] hit MAX_DISPATCH_PER_RUN=%d; remaining rows next run"
                  % MAX_DISPATCH_PER_RUN)
            break
        created = row.get("CreatedDate")
        if row["Id"] in seen:
            # Already handled in a prior run — just let the watermark move past it.
            new_watermark = created or new_watermark
            continue
        status, chat_id, error = dispatch(row)
        try:
            log_dispatch(row, status, chat_id=chat_id, error=error)
        except Exception as e:  # noqa: BLE001
            # If we can't record it, do NOT advance past it (avoid a silent re-dispatch gap).
            print("[log] failed to record %s: %s -> stop advancing watermark" % (row["Id"], e))
            break
        new_watermark = created or new_watermark
        processed += 1
        if status == "dispatched":
            dispatched.append(row["Id"])
        elif status == "dry_run":
            dispatched.append(row["Id"])
        elif status == "skipped_no_bdr":
            skipped.append(row["Id"])
        else:
            failed.append(row["Id"])
        print("[dispatch] %s -> %s chat=%s %s"
              % (row["Id"], status, chat_id, ("(%s)" % error) if error else ""))

    if new_watermark and new_watermark != watermark:
        set_watermark(new_watermark)

    result = {
        "candidates": len(candidates),
        "dispatched": len(dispatched),
        "skipped": len(skipped),
        "failed": len(failed),
        "watermark_from": watermark,
        "watermark_to": new_watermark,
    }
    print("[done] %s" % json.dumps(result))
    return result
