"""One-off: create public.opportunity_observatory and import the 32 dossiers
from the attached Observatory CSV. Idempotent (upsert on opportunity_id).

Run: python3 scripts/import_observatory.py [csv_path]
"""
import csv
import os
import sys

import httpx

csv.field_size_limit(10**9)

CSV_PATH = (
    sys.argv[1] if len(sys.argv) > 1
    else "attached_assets/Observatory_for_Opportunities_-_Set_1_(2)_1780033180343.csv"
)

PROJECT_REF = os.environ["SUPABASE_PROJECT_REF"]
ACCESS_TOKEN = os.environ["SUPABASE_ACCESS_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_SERVICE_KEY"]

MGMT = f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query"

DDL = """
CREATE TABLE IF NOT EXISTS public.opportunity_observatory (
    opportunity_id                  text PRIMARY KEY,
    name                            text,
    opportunity_owner               text,
    close_date                      text,
    amount                          numeric,
    stage                           text,
    account_name                    text,
    sf_90day_evidence               text,
    avoma_evidence                  text,
    outbound_campaign_intelligence  text,
    bundle_a_deal_progress          text,
    bundle_b_competition_fit        text,
    bundle_c_stakeholder_map        text,
    bundle_d_vulnerabilities        text,
    diagnosis_sheet                 text,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    updated_at                      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_obs_account ON public.opportunity_observatory (account_name);
CREATE INDEX IF NOT EXISTS idx_obs_stage   ON public.opportunity_observatory (stage);
CREATE INDEX IF NOT EXISTS idx_obs_search  ON public.opportunity_observatory
    USING gin (to_tsvector('english', coalesce(name,'') || ' ' || coalesce(account_name,'')));
"""

COLMAP = [
    ("Opportunity ID", "opportunity_id"),
    ("Name", "name"),
    ("Opportunity Owner", "opportunity_owner"),
    ("Close Date", "close_date"),
    ("Amount", "amount"),
    ("Stage", "stage"),
    ("Account Name", "account_name"),
    ("SF 90-Day Evidence Pull v2", "sf_90day_evidence"),
    ("Avoma Evidence Pull", "avoma_evidence"),
    ("Outbound & Campaign Intelligence", "outbound_campaign_intelligence"),
    ("Bundle A: Deal Progress & Execution", "bundle_a_deal_progress"),
    ("Bundle B: Competition & Product-Fit", "bundle_b_competition_fit"),
    ("Bundle C: Stakeholder & Confidence Map", "bundle_c_stakeholder_map"),
    ("Bundle D: Vulnerabilities & Open Risks", "bundle_d_vulnerabilities"),
    ("Opportunity Diagnosis Sheet", "diagnosis_sheet"),
]


def _run_ddl():
    r = httpx.post(
        MGMT,
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
        json={"query": DDL},
        timeout=60.0,
    )
    r.raise_for_status()
    print("[DDL] opportunity_observatory ready")


def _parse_amount(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("£", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _load_rows():
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            opp = (raw.get("Opportunity ID") or "").strip()
            if not opp:
                continue
            rec = {}
            for src, dst in COLMAP:
                val = raw.get(src)
                if dst == "amount":
                    rec[dst] = _parse_amount(val)
                else:
                    rec[dst] = (val.strip() if isinstance(val, str) else val) or None
            # merge-upsert keeps existing rows' DB defaults, so set updated_at
            # explicitly to refresh it on every re-import.
            rec["updated_at"] = now_iso
            rows.append(rec)
    return rows


def _upsert(rows):
    headers = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    r = httpx.post(
        f"{SUPABASE_URL}/rest/v1/opportunity_observatory?on_conflict=opportunity_id",
        headers=headers,
        json=rows,
        timeout=120.0,
    )
    r.raise_for_status()
    print(f"[IMPORT] upserted {len(rows)} rows (status {r.status_code})")


if __name__ == "__main__":
    _run_ddl()
    rows = _load_rows()
    print(f"[CSV] parsed {len(rows)} opportunity rows from {CSV_PATH}")
    _upsert(rows)
    print("[DONE]")
