"""READ-ONLY probe: prove the 24h-summary data layer end to end.

Pulls creds from AWS Secrets Manager (mase/app-env) IN-PROCESS (never prints
values), logs into Salesforce via the SOAP partner login (no extra deps), runs a
few read-only SOQL probes against one live forecasted opportunity, and reads the
prod deal_records book from Supabase. Nothing is written anywhere.
"""
import subprocess, json, re, html, sys, datetime as dt
import requests
import urllib3

# Zscaler re-signs TLS on this corp machine and its CA isn't marked critical, which
# Python 3.14's OpenSSL 3.x rejects even with the corp bundle. Traffic is already
# proxy-intercepted on this managed network, so for these LOCAL read-only calls we
# skip Python-side verification. (The AWS CLI has its own ca_bundle configured.)
VERIFY = False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

AWS = r"C:\Program Files\Amazon\AWSCLIV2\aws.exe"
SECRET_ID = "mase/app-env"
REGION = "ap-south-1"
API = "v60.0"


def load_secret() -> dict:
    out = subprocess.check_output(
        [AWS, "secretsmanager", "get-secret-value", "--secret-id", SECRET_ID,
         "--region", REGION, "--query", "SecretString", "--output", "text"],
        text=True)
    return json.loads(out)


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
        raise SystemExit("SF LOGIN FAILED: " + (html.unescape(fault.group(1)) if fault else r.text[:400]))
    surl = re.search(r"<serverUrl>(.*?)</serverUrl>", r.text, re.S)
    session_id = html.unescape(sid.group(1))
    instance = re.match(r"(https://[^/]+)", html.unescape(surl.group(1))).group(1)
    return session_id, instance


def soql(sid: str, instance: str, q: str) -> dict:
    r = requests.get(f"{instance}/services/data/{API}/query/", params={"q": q},
                     headers={"Authorization": f"Bearer {sid}"}, verify=VERIFY, timeout=90)
    if r.status_code != 200:
        return {"_error": f"{r.status_code}: {r.text[:300]}", "records": []}
    return r.json()


def sb_get(sec: dict, path: str):
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    r = requests.get(f"{base}/rest/v1/{path}",
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     verify=VERIFY, timeout=60)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


def main():
    sec = load_secret()
    print("[creds] loaded", len(sec), "keys from", SECRET_ID)

    # --- Supabase: read the live forecasted book -------------------------------
    code, rows = sb_get(sec, "deal_records?select=opp_id,account_name,opp_name,owner_name,forecast_category,stage&limit=8")
    print("\n[supabase] deal_records flat-select status:", code)
    flat_ok = isinstance(rows, list) and rows and "forecast_category" in rows[0]
    if not flat_ok:
        code, rows = sb_get(sec, "deal_records?select=opp_id,account_name,opp_name,owner_name,record&limit=8")
        print("[supabase] fallback record-select status:", code)
    sample = []
    for r in (rows if isinstance(rows, list) else []):
        rec = r.get("record") or {}
        hard = rec.get("hard") or {}
        sample.append({
            "opp_id": r.get("opp_id"),
            "account": r.get("account_name") or hard.get("account_name"),
            "owner": r.get("owner_name") or hard.get("owner_name"),
            "forecast": r.get("forecast_category") or hard.get("forecast_category"),
            "stage": r.get("stage") or hard.get("stage"),
        })
    print("[supabase] sample deal_records:")
    for s in sample:
        print("   ", s["opp_id"], "|", (s["account"] or "")[:28].ljust(28),
              "| fc:", str(s["forecast"])[:16].ljust(16), "| owner:", s["owner"])

    if not sample:
        print("!! no deal_records returned; stopping probe")
        return

    probe_opp = sample[0]["opp_id"]
    print(f"\n[probe opp] {probe_opp}  ({sample[0]['account']})")

    # --- Salesforce: prove connectivity + activity/movement pulls ---------------
    sid, instance = sf_login(sec)
    print("[sf] login OK, instance:", instance)

    now = dt.datetime.now(dt.timezone.utc)
    since24 = (now - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("[sf] now(UTC):", now.strftime("%Y-%m-%dT%H:%M:%SZ"), "| 24h window since:", since24)

    oid = probe_opp
    probes = {
        "OpportunityFieldHistory (movements, last 10)":
            f"SELECT Field, OldValue, NewValue, CreatedDate, CreatedBy.Name FROM OpportunityFieldHistory WHERE OpportunityId = '{oid}' ORDER BY CreatedDate DESC LIMIT 10",
        "Task (last 10 by LastModified)":
            f"SELECT Id, Subject, Status, TaskSubtype, Type, ActivityDate, CreatedDate, LastModifiedDate, CompletedDateTime, Owner.Name FROM Task WHERE WhatId = '{oid}' ORDER BY LastModifiedDate DESC LIMIT 10",
        "Event (last 5)":
            f"SELECT Id, Subject, ActivityDateTime, CreatedDate, Owner.Name FROM Event WHERE WhatId = '{oid}' ORDER BY ActivityDateTime DESC NULLS LAST LIMIT 5",
        "EmailMessage (last 5)":
            f"SELECT Id, Subject, MessageDate, Incoming, FromAddress FROM EmailMessage WHERE RelatedToId = '{oid}' ORDER BY MessageDate DESC LIMIT 5",
        "Opportunity next-step fields":
            f"SELECT Id, Name, StageName, Amount, CloseDate, ForecastCategoryName, Next_Step__c, Next_Step_Updated_Date_Time__c, LastActivityDate FROM Opportunity WHERE Id = '{oid}' LIMIT 1",
    }
    for label, q in probes.items():
        res = soql(sid, instance, q)
        if res.get("_error"):
            print(f"\n[sf] {label}: ERROR {res['_error']}")
            continue
        recs = res.get("records", [])
        print(f"\n[sf] {label}: {len(recs)} rows")
        for rec in recs[:5]:
            rec.pop("attributes", None)
            print("     ", json.dumps(rec, default=str)[:220])


if __name__ == "__main__":
    main()
