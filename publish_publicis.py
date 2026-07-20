"""Publish a COMPLETE Publicis record to the deal drawer (deal_records), generated locally.

Base = the rich studio-v2 canonical record (cc_work/006P700000Xl06R.studio.final.json:
MEDDPICC, day_summary, moves, requirements, deal_scores_evidence, stakeholder map...).
Upgrade the HEADLINE with the Studio-governed AI scorer (direct Anthropic call, Zscaler-safe;
factor_source=ai), rebuild the CRO reasons panel on the new scores, stamp Omnivision
provenance, then upsert with the exact production row shape. Best-effort AI score: if the
call fails, the deterministic scores already in the record stand (still complete).

Run:  python publish_publicis.py            (dry — prints what would push)
      python publish_publicis.py --push     (writes to deal_records)
"""
import sys, os, json, datetime, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, load_datalake, VERIFY, sb_upsert
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PUSH = "--push" in sys.argv
OID15 = "006P700000Xl06R"
SRC = "cc_work/006P700000Xl06R.studio.final.json"

sec = load_secret()
for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY"):
    if sec.get(k):
        os.environ[k] = sec[k]
dl = load_datalake()
os.environ["DATALAKE_URL"] = dl["DATALAKE_URL"]; os.environ["DATALAKE_SERVICE_KEY"] = dl["DATALAKE_SERVICE_KEY"]
os.environ.setdefault("DEAL_ENGINE_AI_SCORING", "true")
SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}

# --- locked Studio versions, requests-shim (httpx fails locally through Zscaler) ---
import scoring_studio as st


def active_locked_local():
    rows = requests.get(f"{SB}/rest/v1/scoring_instructions",
                        params={"locked": "eq.true", "select": "engine,version,content"},
                        headers=H, verify=VERIFY, timeout=60).json()
    def vk(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except ValueError:
            return (-1,)
    best = {}
    for r in rows:
        if r["version"] == "draft":
            continue
        e = r["engine"]
        if e not in best or vk(r["version"]) > vk(best[e]["version"]):
            best[e] = r
    return {e: ({"version": best[e]["version"], "content": best[e]["content"]} if e in best else None)
            for e in st.ASSETS}


st.active_locked = active_locked_local
_active = active_locked_local()
_versions = {e: v["version"] for e, v in _active.items() if v}

import deal_engine_ai_scoring as A
import deal_engine_evidence as EV
import deal_engine_cro as CRO
from opportunity_analyzer import _extract_json

rec = json.load(open(SRC, encoding="utf-8"))
ai = rec.setdefault("ai", {})
det = (ai.get("deal_scores") or {}).get("headline") or {}
print(f"base record: win={det.get('win_position')} mom={det.get('deal_momentum')} "
      f"(deterministic) | meddpicc={len(ai.get('meddpicc') or {})} moves="
      f"{len((ai.get('recommended_moves') or {}).get('items') or [])} day_summary={bool(ai.get('day_summary'))}")

# --- Studio-governed AI score via a DIRECT Anthropic call (best-effort) ---
ai_ok = False
try:
    packet = EV.build_evidence_packet(rec)
    sysp = A._prompt()   # win+mom engines (via shim) + output adapter
    user = "Score this opportunity. Evidence packet (facts only):\n\n" + json.dumps(packet, default=str, ensure_ascii=False)
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": sec["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json={"model": (os.getenv("DEAL_ENGINE_SCORING_MODEL") or "anthropic:claude-sonnet-5").split(":")[-1],
                            "max_tokens": 16000, "system": sysp,
                            "messages": [{"role": "user", "content": user}]},
                      verify=False, timeout=420)
    out = r.json()
    text = "".join(b.get("text", "") for b in out.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "text")
    parsed = _extract_json(text)
    if isinstance(parsed, dict) and parsed.get("scores"):
        scored = A._normalize(parsed, packet)   # deal_scores shape, factor_source=ai, ai_reasons
        # keep the rich subagent narratives; swap in the AI headline + reasons + source
        prev_ds = ai.get("deal_scores") or {}
        scored["cro_panel"] = prev_ds.get("cro_panel")   # rebuilt below
        ai["deal_scores"] = scored
        # refresh deal_scores_evidence with the AI reasons so the drawer panel is deal-specific
        ai["deal_scores_evidence"] = {
            "summary": (parsed.get("read") or ""),
            "ai_reasons": scored.get("ai_reasons") or {},
        }
        ai_ok = True
        hl = scored["headline"]
        print(f"AI score: win={hl.get('win_position')} mom={hl.get('deal_momentum')} "
              f"risk={hl.get('deal_risk')} read={hl.get('read')} | factor_source=ai")
    else:
        print(f"AI score UNUSABLE (chars={len(text)}) — keeping deterministic scores")
except Exception as e:
    print(f"AI score failed ({type(e).__name__}: {str(e)[:120]}) — keeping deterministic scores")

# --- rebuild CRO reasons panel on the (now AI-)scored record ---
try:
    panel = CRO.build_cro_panel(rec)
    if panel:
        ai.setdefault("deal_scores", {})["cro_panel"] = panel
        print(f"cro_panel rebuilt: {len(panel.get('blocks') or [])} blocks")
except Exception as e:
    print(f"cro_panel rebuild skipped: {e}")

# --- provenance + freshness ---
ai["scoring_studio"] = {"versions": _versions,
                        "stamped_at": datetime.date.today().isoformat()}
rec["swept_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
rec["opp_id"] = OID15
hard = rec.get("hard") or {}

hl = (ai.get("deal_scores") or {}).get("headline") or {}
print("\n=== WOULD PUBLISH ===")
print("win/mom:", hl.get("win_position"), "/", hl.get("deal_momentum"),
      "| factor_source:", (ai.get("deal_scores") or {}).get("factor_source"))
print("provenance:", json.dumps(_versions))
print("drawer surfaces -> day_summary:", bool(ai.get("day_summary")),
      "| cro_panel blocks:", len(((ai.get("deal_scores") or {}).get("cro_panel") or {}).get("blocks") or []),
      "| moves:", len((ai.get("recommended_moves") or {}).get("items") or []),
      "| explicit_req:", len((ai.get("explicit_requirements") or {}).get("items") or []),
      "| meddpicc:", len(ai.get("meddpicc") or {}))

if not PUSH:
    json.dump(rec, open("cc_work/006P700000Xl06R.publish.json", "w", encoding="utf-8"), indent=1, default=str)
    print("\n[DRY] wrote cc_work/006P700000Xl06R.publish.json — re-run with --push to write to deal_records")
    sys.exit(0)

# --- upsert: EXACT production row shape (deal_engine_store.upsert_record mirror cols) ---
row = {
    "opp_id": OID15, "owner_name": hard.get("owner_name"), "account_name": hard.get("account_name"),
    "opp_name": hard.get("opp_name"), "stage": hard.get("stage"),
    "forecast_category": hard.get("forecast_category"), "amount": hard.get("amount"),
    "close_date": hard.get("close_date") or None, "qualified_date": hard.get("qualified_date") or None,
    "last_activity_date": hard.get("last_activity_date") or None,
    "forecast_critical": bool(rec.get("forecast_critical")),
    "analysis_confidence": rec.get("analysis_confidence"), "swept_at": rec.get("swept_at"),
    "record": rec, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
sb_upsert(sec, "deal_records", [row], on_conflict="opp_id")
print("\nPUSHED to deal_records (opp_id=006P700000Xl06R).")

# verify
v = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "record,swept_at", "opp_id": f"eq.{OID15}"},
                 headers=H, verify=VERIFY, timeout=60).json()[0]
vai = v["record"].get("ai") or {}; vds = vai.get("deal_scores") or {}; vhl = vds.get("headline") or {}
print("VERIFY DB -> swept_at:", v["swept_at"], "| win/mom:", vhl.get("win_position"), "/", vhl.get("deal_momentum"),
      "| factor_source:", vds.get("factor_source"), "| day_summary:", bool(vai.get("day_summary")),
      "| panel blocks:", len((vds.get("cro_panel") or {}).get("blocks") or []))
