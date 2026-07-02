"""Shared helpers for the 24h-summary feature — all LOCAL and read-only unless a
caller explicitly upserts. Creds come from AWS Secrets Manager (mase/app-env); no
secret value is ever printed. Salesforce is reached via its REST/SOAP API with
plain `requests` (no simple_salesforce dependency); Supabase via PostgREST.

Zscaler note: this corp machine re-signs TLS and its CA isn't marked critical,
which Python 3.14's OpenSSL rejects. Traffic is already proxy-intercepted, so for
these local calls we skip Python-side verification (VERIFY=False). The AWS CLI has
its own configured ca_bundle.
"""
from __future__ import annotations
import subprocess, json, re, html, os, shutil, datetime as dt
import requests, urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# TLS verification: OFF only on the corp Windows laptop (Zscaler re-signs TLS with a
# CA that Python 3.14's OpenSSL rejects; traffic is already proxy-intercepted). ON
# everywhere else (Linux CI / ECS container have no interception). Override with
# DS_TLS_VERIFY=1|0 if a run's environment differs.
VERIFY = os.getenv("DS_TLS_VERIFY", "0" if os.name == "nt" else "1") == "1"

# AWS CLI: use whatever is on PATH (Linux CI / container), falling back to the
# Windows install path for the local laptop. In CI, aws-actions/configure-aws-
# credentials provides the role creds the CLI reads. In ECS the app-env keys are
# ALSO injected as env vars, so load_secret prefers those and skips the CLI entirely.
AWS = shutil.which("aws") or r"C:\Program Files\Amazon\AWSCLIV2\aws.exe"
SECRET_ID = "mase/app-env"
REGION = "ap-south-1"
# The mase/app-env keys this batch needs. When ALL are already present in the
# process env (the ECS container case — the task def injects them from Secrets
# Manager), load_secret reads env and never shells out to the AWS CLI.
_REQUIRED_SECRET_KEYS = ("SF_USERNAME", "SF_PASSWORD", "SUPABASE_URL")
API = "v60.0"
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- creds -------------------------------------------------------------------
_SECRET_CACHE: dict | None = None


def load_secret() -> dict:
    global _SECRET_CACHE
    if _SECRET_CACHE is None:
        # Container case: the app-env keys are already in the process env (ECS
        # injects them). Read env directly — no AWS CLI needed, no extra IAM.
        if all(os.environ.get(k) for k in _REQUIRED_SECRET_KEYS):
            _SECRET_CACHE = {k: v for k, v in os.environ.items()}
        else:
            # Local laptop / CI: pull the whole secret via the AWS CLI (creds come
            # from the machine profile locally, or the assumed OIDC role in CI).
            out = subprocess.check_output(
                [AWS, "secretsmanager", "get-secret-value", "--secret-id", SECRET_ID,
                 "--region", REGION, "--query", "SecretString", "--output", "text"],
                text=True)
            _SECRET_CACHE = json.loads(out)
    return _SECRET_CACHE


def load_datalake() -> dict:
    """Parse the gitignored .datalake_secrets.env for Avoma datalake creds."""
    path = os.path.join(_REPO, ".datalake_secrets.env")
    out: dict = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# --- tiny utils --------------------------------------------------------------
def id15(x: str | None) -> str:
    return (x or "")[:15]


def iso_z(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_sf(ts: str | None):
    if not ts:
        return None
    t = ts.replace("+0000", "+00:00").replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(t)
    except Exception:
        return None


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<\s*br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"</?p\s*>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# --- Salesforce --------------------------------------------------------------
def sf_login(sec: dict):
    dom = (sec.get("SF_DOMAIN") or "login").strip()
    host = "login" if dom in ("login", "") else dom
    url = f"https://{host}.salesforce.com/services/Soap/u/60.0"
    user = html.escape(sec["SF_USERNAME"])
    pw = html.escape((sec["SF_PASSWORD"] or "") + (sec.get("SF_SECURITY_TOKEN") or ""))
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<se:Envelope xmlns:se="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:urn="urn:partner.soap.sforce.com"><se:Body><urn:login>'
        f'<urn:username>{user}</urn:username><urn:password>{pw}</urn:password>'
        '</urn:login></se:Body></se:Envelope>')
    r = requests.post(url, data=body.encode("utf-8"),
                      headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "login"},
                      verify=VERIFY, timeout=60)
    sid = re.search(r"<sessionId>(.*?)</sessionId>", r.text, re.S)
    if not sid:
        fault = re.search(r"<faultstring>(.*?)</faultstring>", r.text, re.S)
        raise RuntimeError("SF login failed: " + (html.unescape(fault.group(1)) if fault else r.text[:300]))
    surl = re.search(r"<serverUrl>(.*?)</serverUrl>", r.text, re.S)
    session_id = html.unescape(sid.group(1))
    instance = re.match(r"(https://[^/]+)", html.unescape(surl.group(1))).group(1)
    return session_id, instance


def soql(sid: str, instance: str, q: str) -> list:
    """Run SOQL, following nextRecordsUrl pagination. Raises on non-200."""
    recs: list = []
    url = f"{instance}/services/data/{API}/query/"
    params = {"q": q}
    while True:
        r = requests.get(url, params=params, headers={"Authorization": f"Bearer {sid}"},
                         verify=VERIFY, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"SOQL {r.status_code}: {r.text[:300]} | Q={q[:160]}")
        j = r.json()
        recs += j.get("records", [])
        nxt = j.get("nextRecordsUrl")
        if not nxt:
            return recs
        url = f"{instance}{nxt}"
        params = None


def soql_in(ids, tmpl: str, chunk: int = 200) -> str:
    """Build an IN(...) clause value; caller inserts into a query template.
    (SF matches 15-char ids against 18-char storage, so 15-char ids are fine.)"""
    return "(" + ",".join("'" + i + "'" for i in ids) + ")"


# --- Supabase (prod deal DB) -------------------------------------------------
def _sb_base(sec):
    return sec["SUPABASE_URL"].rstrip("/"), (sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY"))


def sb_get(sec: dict, path: str):
    base, key = _sb_base(sec)
    r = requests.get(f"{base}/rest/v1/{path}",
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     verify=VERIFY, timeout=60)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


def sb_upsert(sec: dict, table: str, rows: list, on_conflict: str):
    base, key = _sb_base(sec)
    r = requests.post(f"{base}/rest/v1/{table}",
                      params={"on_conflict": on_conflict},
                      headers={"apikey": key, "Authorization": f"Bearer {key}",
                               "Content-Type": "application/json",
                               "Prefer": "resolution=merge-duplicates,return=representation"},
                      data=json.dumps(rows), verify=VERIFY, timeout=90)
    if r.status_code >= 300:
        raise RuntimeError(f"upsert {r.status_code}: {r.text[:300]}")
    return r.json()


def datalake_get(dl: dict, path: str):
    base = (dl.get("DATALAKE_URL") or "").rstrip("/")
    key = dl.get("DATALAKE_SERVICE_KEY")
    if not base or not key:
        return None
    r = requests.get(f"{base}/rest/v1/{path}",
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     verify=VERIFY, timeout=60)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None
