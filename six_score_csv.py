"""Score the 6 already-synthesized deals (cc_work/<oid>.json) in PARALLEL via the direct
Studio scorer (win 10.7 / mom 10.7, raw Anthropic REST — Zscaler-safe, production API, NOT
Claude-Code credits), run the REAL CEO/CRO finalize, write cc_work/<oid>.final.json, and emit
cc_fleet_results.csv + print scores + reasons. No deal_records writes."""
import json, os, csv, time, datetime as dt, warnings, concurrent.futures as cf
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import cc_sweep
from daily_summary.common import VERIFY

sec = cc_sweep.load_env()
os.environ["ANTHROPIC_API_KEY"] = sec["ANTHROPIC_API_KEY"]
try:
    from daily_summary.common import load_datalake
    _dl = load_datalake()
    os.environ.setdefault("DATALAKE_URL", _dl["DATALAKE_URL"])
    os.environ.setdefault("DATALAKE_SERVICE_KEY", _dl["DATALAKE_SERVICE_KEY"])
except Exception:
    pass
SB = sec["SUPABASE_URL"].rstrip("/")
K = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": K, "Authorization": f"Bearer {K}"}
AK = sec["ANTHROPIC_API_KEY"]

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
        if r.get("version") == "draft":
            continue
        e = r["engine"]
        if e not in best or vk(r["version"]) > vk(best[e]["version"]):
            best[e] = r
    return {e: ({"version": best[e]["version"], "content": best[e]["content"]} if e in best else None)
            for e in st.ASSETS}


st.active_locked = active_locked_local
import deal_engine_ai_scoring as A
import deal_engine_evidence as EV
import deal_engine_cro as CRO
import deal_engine_ceo as CEO
import deal_engine_validation as V
from opportunity_analyzer import _extract_json
SYSP = A._prompt()

SIX = [("SAMI", "006P700000RD9Ir"), ("Allstate", "006P7000006uKrq"),
       ("Robert Bosch GmbH", "006P700000PlMpu"), ("NORTHPORT (MALAYSIA)", "006P700000QFJwD"),
       ("Domino's Pizza", "006P700000X6hvK"), ("Greencore Group", "006P700000WeRX8")]


def ai_score(rec):
    packet = EV.build_evidence_packet(rec)
    user = "Score this opportunity. Evidence packet (facts only):\n\n" + json.dumps(packet, default=str, ensure_ascii=False)
    for att in range(5):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": AK, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                              json={"model": "claude-sonnet-5", "max_tokens": 16000, "system": SYSP,
                                    "messages": [{"role": "user", "content": user}]},
                              verify=False, timeout=600)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(15 * (att + 1)); continue
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            parsed = _extract_json(text)
            if isinstance(parsed, dict) and parsed.get("scores"):
                return A._normalize(parsed, packet), packet
        except requests.RequestException:
            time.sleep(10 * (att + 1))
    return None, None


def score_one(label, oid):
    try:
        raw = open(f"cc_work/{oid}.json", encoding="utf-8").read()
        parsed = _extract_json(raw) if not raw.strip().startswith("{") else json.loads(raw)
        ctx = json.load(open(f"cc_work/{oid}.ctx.json", encoding="utf-8"))
        rec, viol = cc_sweep.postprocess(parsed, ctx["opp"], ctx["buyer"], ctx["existing"])
        ds, packet = ai_score(rec)
        if ds:
            rec["ai"]["deal_scores"] = ds
            try:
                allow = V.build_people_allowlist(ctx["buyer"], ctx.get("existing") or {})
                CEO.finalize_ceo_intervention(rec, ctx["opp"], ctx["buyer"],
                                              prior_ai=(ctx.get("existing") or {}).get("ai"), allowlist=allow)
            except Exception:
                pass
            try:
                panel = CRO.build_cro_panel(rec)
                if panel:
                    rec["ai"]["deal_scores"]["cro_panel"] = panel
            except Exception:
                pass
        rec["account_name"] = ctx["opp"].get("account")
        rec["opp_name"] = ctx["opp"].get("name")
        rec["swept_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        json.dump(rec, open(f"cc_work/{oid}.final.json", "w", encoding="utf-8"), indent=1, default=str)
        ai = rec.get("ai") or {}
        ds2 = ai.get("deal_scores") or {}
        hl = ds2.get("headline") or {}
        rz = ds2.get("ai_reasons") or {}
        ceo = ai.get("ceo_intervention") or {}
        ev = rec.get("evidence_coverage") or {}
        d24 = ai.get("day_summary") or {}
        fr = ai.get("forecast_read") or {}

        def rr(key):
            return " || ".join(f"[{b.get('tone')}] {b.get('text')}" for b in (rz.get(key) or []))
        return {
            "account": label, "opp_id": oid, "opp_name": rec.get("opp_name"),
            "stage": (rec.get("hard") or {}).get("stage"),
            "forecast_category": (rec.get("hard") or {}).get("forecast_category"),
            "amount": (rec.get("hard") or {}).get("amount"),
            "close_date": (rec.get("hard") or {}).get("close_date"),
            "win": hl.get("win_position"), "momentum": hl.get("deal_momentum"),
            "commitment": hl.get("customer_commitment"), "risk": hl.get("deal_risk"),
            "forecast_confidence": hl.get("forecast_confidence"), "read": hl.get("read"),
            "factor_source": ds2.get("factor_source"),
            "win_reasons": rr("win_position"), "momentum_reasons": rr("deal_momentum"),
            "commitment_reasons": rr("customer_commitment"), "risk_reasons": rr("deal_risk"),
            "forecast_defensible": fr.get("defensible"), "forecast_recommended": fr.get("recommended_forecast"),
            "day_summary": (d24.get("overall") or "")[:500],
            "ceo_needed": ceo.get("needed"), "ceo_severity": ceo.get("severity"),
            "ceo_summary": (ceo.get("summary") or "")[:400],
            "stakeholders_n": len(((ai.get("stakeholder_map") or {}).get("items")) or []),
            "moves_n": len(((ai.get("recommended_moves") or {}).get("items")) or []),
            "competitors": "; ".join(str(c.get("name")) for c in ((ai.get("competitive_position") or {}).get("competitors") or [])),
            "calls_discovered": ev.get("calls_discovered"), "calls_read": ev.get("calls_read"),
            "confidence": ev.get("confidence") or ai.get("analysis_confidence"),
            "violations": viol,
        }
    except Exception as e:
        return {"account": label, "opp_id": oid, "error": f"{type(e).__name__}: {e}"}


print(f"scorer prompt {len(SYSP)}c | scoring {len(SIX)} deals in parallel…", flush=True)
with cf.ThreadPoolExecutor(max_workers=6) as ex:
    rows = list(ex.map(lambda t: score_one(*t), SIX))

FIELDS = ["account", "opp_id", "opp_name", "stage", "forecast_category", "amount", "close_date",
          "win", "momentum", "commitment", "risk", "forecast_confidence", "read", "factor_source",
          "win_reasons", "momentum_reasons", "commitment_reasons", "risk_reasons",
          "forecast_defensible", "forecast_recommended", "day_summary",
          "ceo_needed", "ceo_severity", "ceo_summary", "stakeholders_n", "moves_n", "competitors",
          "calls_discovered", "calls_read", "confidence"]
with open("cc_fleet_results.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)

print("\n================ SCORES ================", flush=True)
for r in rows:
    if r.get("error"):
        print(f"\n{r['account']}: ERROR {r['error']}", flush=True)
        continue
    print(f"\n### {r['account']} — {r['stage']} · ${r['amount']} — WIN {r['win']} | MOM {r['momentum']} | "
          f"commit {r['commitment']} | risk {r['risk']} | read {r['read']} (src {r['factor_source']})", flush=True)
    for key, lbl in (("win_reasons", "WIN"), ("momentum_reasons", "MOM")):
        print(f"  {lbl}:", flush=True)
        for b in (r.get(key) or "").split(" || "):
            if b.strip():
                print(f"    • {b}", flush=True)
    print(f"  CEO: needed={r['ceo_needed']} sev={r['ceo_severity']} — {r['ceo_summary'][:140]}", flush=True)
print(f"\nCSV: cc_fleet_results.csv ({len([r for r in rows if not r.get('error')])} scored)", flush=True)
