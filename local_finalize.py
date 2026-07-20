"""Finalize the LOCAL cc_sweep records for the deal drawer, fully offline:
  1. cc_sweep.postprocess -> REAL production guardrails / roster / CEO (byte-for-byte).
  2. Studio AI scores (win 10.5 / mom 10.6) via raw Anthropic REST (verify=False, Zscaler-safe).
  3. CRO panel (reads ai_reasons -> rich per-score bullets).
  4. Print EVERY deal-drawer surface + write a LOCAL-vs-CLOUD comparison CSV.
NOTHING is written to deal_records.
"""
import os, sys, json, csv, time, textwrap, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import cc_sweep
from daily_summary.common import VERIFY
sec = cc_sweep.load_env()
os.environ["ANTHROPIC_API_KEY"] = sec["ANTHROPIC_API_KEY"]
try:
    from daily_summary.common import load_datalake
    _dl = load_datalake()
    os.environ.setdefault("DATALAKE_URL", _dl["DATALAKE_URL"])
    os.environ.setdefault("DATALAKE_SERVICE_KEY", _dl["DATALAKE_SERVICE_KEY"])
except Exception as _e:
    print("datalake env unavailable (packet uses record's stored calls):", _e)
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
from opportunity_analyzer import _extract_json
SYSP = A._prompt()
print(f"local finalize | scorer prompt {len(SYSP)}c Studio-governed={'GOVERNING ENGINE' in SYSP}", flush=True)

_NAMES = {"006P7000006uKrq": "Allstate", "006P700000OcxpH": "Consumer Cellular",
          "006P700000DkWgX": "ACEN", "0066700000wdNe1": "Cebu Pacific Air"}
_ids = [a for a in sys.argv[1:] if a[:2] in ("00", "0P") or a.startswith("006")]
OPPS = [(_NAMES.get(a, a), a) for a in _ids] if _ids else \
    [("Allstate", "006P7000006uKrq"), ("Consumer Cellular", "006P700000OcxpH")]


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
                time.sleep(20 * (att + 1)); continue
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            parsed = _extract_json(text)
            if isinstance(parsed, dict) and parsed.get("scores"):
                return A._normalize(parsed, packet)
        except requests.RequestException:
            time.sleep(15 * (att + 1))
    return None


def wrap(s, width=104):
    s = "" if s is None else str(s)
    return "\n     ".join(textwrap.wrap(s, width)) if len(s) > width else s


def bullets(items, fmt):
    for it in (items or []):
        try:
            print("   - " + wrap(fmt(it)))
        except Exception as e:
            print(f"   - <err {e}>")


def print_drawer(rec):
    hard = rec.get("hard") or {}; ai = rec.get("ai") or {}
    ds = ai.get("deal_scores") or {}; hl = ds.get("headline") or {}; ev = rec.get("evidence_coverage") or {}
    print("\n" + "#" * 96)
    print(f"# {rec.get('account_name') or hard.get('account_name') or hard.get('account')}  —  {rec.get('opp_name')}  ({rec.get('opp_id')})")
    print("#" * 96)
    print("\n== HEADER ==")
    for k in ("stage", "amount", "close_date", "forecast_category", "owner_name", "last_activity_date"):
        print(f"  {k}: {hard.get(k)}")
    print(f"  next_step: {wrap(hard.get('next_step'))}")
    print("\n== EVIDENCE COVERAGE ==")
    print(f"  calls_discovered={ev.get('calls_discovered')} calls_read={ev.get('calls_read')} "
          f"method={ev.get('discovery_method')} gaps={ev.get('gaps')} confidence={ev.get('confidence') or ai.get('analysis_confidence')}")
    print("\n== DEAL SCORES ==")
    print(f"  WIN {hl.get('win_position')} | MOMENTUM {hl.get('deal_momentum')} | read={hl.get('read')} | "
          f"commit={hl.get('customer_commitment')} risk={hl.get('deal_risk')} forecast_conf={hl.get('forecast_confidence')} "
          f"| src={ds.get('factor_source')}")
    reasons = ds.get("ai_reasons") or {}
    for key in ("win_position", "deal_momentum", "customer_commitment", "deal_risk"):
        rs = reasons.get(key) or []
        if rs:
            print(f"  -- {key} ({len(rs)}) --")
            bullets(rs, lambda b: f"[{b.get('tone')}] {b.get('text')}")
    print("\n== 24-HOUR SUMMARY ==")
    d24 = ai.get("day_summary") or {}
    print(f"  overall: {wrap(d24.get('overall'))}")
    bullets(d24.get("items"), lambda it: f"{it.get('at')} [{it.get('kind')}] {it.get('name')}: {it.get('summary')}")
    print("\n== FORECAST READ ==")
    fr = ai.get("forecast_read") or {}
    print(f"  defensible={fr.get('defensible')} recommended={fr.get('recommended_forecast')}")
    print(f"  reason: {wrap(fr.get('reason'))}")
    print("\n== MEDDPICC ==")
    md = ai.get("meddpicc") or {}
    if isinstance(md, dict):
        for k, v in md.items():
            val = v.get("value") or v.get("text") or v.get("summary") or (json.dumps(v)[:220]) if isinstance(v, dict) else v
            print(f"  {k}: {wrap(val)}")
    print("\n== STAKEHOLDER MAP ==")
    stk = (ai.get("stakeholder_map") or {}).get("items") if isinstance(ai.get("stakeholder_map"), dict) else ai.get("stakeholders")
    bullets(stk, lambda s: f"{s.get('name')} ({s.get('title')}) — {s.get('role')} | {s.get('sentiment')} | risk={s.get('risk')} | last={s.get('last_contact_date')}")
    print("\n== COMPETITIVE POSITION ==")
    cp = ai.get("competitive_position") or {}
    print(f"  summary: {wrap(cp.get('summary'))}")
    bullets(cp.get("competitors"), lambda c: f"{c.get('name')} — {c.get('sentiment')}/{c.get('threat_level')} ({c.get('status')}) | how_we_win: {c.get('how_we_win')}")
    print("\n== CRITICAL SIGNALS ==")
    bullets(ai.get("critical_signals"), lambda s: f"[{s.get('tone')}] {s.get('lens')}: {s.get('text')}")
    print("\n== RECOMMENDED MOVES (to-dos) ==")
    rm = (ai.get("recommended_moves") or {}).get("items") if isinstance(ai.get("recommended_moves"), dict) else ai.get("recommended_moves")
    bullets(rm, lambda m: f"[r{m.get('rank')}] ({m.get('horizon')}) {m.get('action')} — act_by {m.get('act_by')} | {m.get('expected_effect')}")
    print("\n== REQUIREMENTS ==")
    ir = ai.get("implicit_requirements") or {}
    for sub in ("we_promised", "buyer_dependent"):
        items = (ir.get(sub) or {}).get("items") if isinstance(ir.get(sub), dict) else None
        if items:
            print(f"  {sub}:"); bullets(items, lambda x: f"{x.get('deliverable')} — due {x.get('due')} ({x.get('status')})")
    er = ai.get("explicit_requirements") or {}
    if isinstance(er, dict) and er.get("items"):
        print("  explicit:"); bullets(er["items"], lambda x: f"{x.get('requirement') or x.get('deliverable')} ({x.get('status')})")
    print("\n== VULNERABILITIES ==")
    vul = ai.get("vulnerabilities")
    if isinstance(vul, list):
        bullets(vul, lambda v: v if isinstance(v, str) else json.dumps(v)[:200])
    print("\n== CEO INTERVENTION ==")
    ceo = ai.get("ceo_intervention") or {}
    print(f"  needed={ceo.get('needed')} | {wrap(ceo.get('summary') or ceo.get('rationale'))}")


rows = []
for label, oid in OPPS:
    jp = f"cc_work/{oid}.json"
    cp = f"cc_work/{oid}.ctx.json"
    if not os.path.exists(jp) or os.path.getsize(jp) < 50:
        print(f"\n{label} ({oid}): subagent JSON not ready yet — skip", flush=True)
        continue
    raw = open(jp, encoding="utf-8").read()
    try:
        parsed = _extract_json(raw) if not raw.strip().startswith("{") else json.loads(raw)
    except Exception as e:
        print(f"{label}: bad subagent JSON: {e}"); continue
    ctx = json.load(open(cp, encoding="utf-8"))
    try:
        rec, viol = cc_sweep.postprocess(parsed, ctx["opp"], ctx["buyer"], ctx["existing"])
    except Exception as e:
        print(f"{label}: postprocess FAILED: {type(e).__name__}: {e}"); continue
    ds = ai_score(rec)
    if ds:
        rec["ai"]["deal_scores"] = ds
        try:
            panel = CRO.build_cro_panel(rec)
            if panel:
                rec["ai"]["deal_scores"]["cro_panel"] = panel
        except Exception as e:
            print(f"   [cro] {e}")
    rec["account_name"] = ctx["opp"].get("account")
    rec["opp_name"] = ctx["opp"].get("name")
    json.dump(rec, open(f"cc_work/{oid}.final.json", "w", encoding="utf-8"), indent=2, default=str)
    print_drawer(rec)
    hl = ((rec.get("ai") or {}).get("deal_scores") or {}).get("headline") or {}
    # cloud comparison (if the cloud dry-run has landed)
    cloud = {}
    cpath = f"dryrun_forecasted/{oid}.json"
    if os.path.exists(cpath):
        try:
            crec = json.load(open(cpath, encoding="utf-8"))
            cloud = ((crec.get("ai") or {}).get("deal_scores") or {}).get("headline") or {}
        except Exception:
            pass
    rows.append({"label": label, "opp_id": oid,
                 "local_win": hl.get("win_position"), "local_mom": hl.get("deal_momentum"), "local_read": hl.get("read"),
                 "cloud_win": cloud.get("win_position"), "cloud_mom": cloud.get("deal_momentum"), "cloud_read": cloud.get("read"),
                 "violations": len(viol)})

if rows:
    os.makedirs("dryrun_local", exist_ok=True)
    with open("dryrun_local/_local_vs_cloud.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print("\n=== LOCAL vs CLOUD ===")
    for r in rows:
        print(f"  {r['label']:20} LOCAL win={r['local_win']} mom={r['local_mom']} ({r['local_read']})  |  "
              f"CLOUD win={r['cloud_win']} mom={r['cloud_mom']} ({r['cloud_read']})  | viol={r['violations']}")
    print("\nCSV: dryrun_local/_local_vs_cloud.csv")
