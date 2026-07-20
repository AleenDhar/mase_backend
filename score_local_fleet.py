"""LOCAL, PARALLEL, Zscaler-safe re-score through the Studio-governed AI scorer.

For each opp: read its record from Supabase, build the SAME evidence packet the deployed
scorer uses, and score via a direct Anthropic REST call (verify=False, max_tokens=16000,
thinking-aware) under the LOCKED Omnivision win/mom engines. Emits win/mom + full reasons
locally under dryrun_local/. NOTHING is written to deal_records — pure local read + score.

This is a RE-SCORE (uses each record's stored analysis); deals with no record are flagged
NEEDS-SWEEP. Purpose: see the real win/momentum + reason DETAIL and compare to the ACEN bar.

Usage:  python score_local_fleet.py
"""
import sys, os, json, csv, time, warnings, concurrent.futures as cf
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
try:
    from daily_summary.common import load_datalake
except Exception:
    load_datalake = None
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret()
for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY", "ANTHROPIC_API_KEY"):
    if sec.get(k):
        os.environ[k] = sec[k]
# datalake env so build_evidence_packet can pull the whole call history for the packet
if load_datalake:
    try:
        dl = load_datalake()
        os.environ.setdefault("DATALAKE_URL", dl["DATALAKE_URL"])
        os.environ.setdefault("DATALAKE_SERVICE_KEY", dl["DATALAKE_SERVICE_KEY"])
    except Exception as e:
        print("datalake creds unavailable (packet uses stored meetings only):", e)

SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
AK = sec["ANTHROPIC_API_KEY"]

# --- Studio governance, read Zscaler-safe via requests (same shim probe_ai_scorer uses) ----
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
from opportunity_analyzer import _extract_json

SYSP = A._prompt()
print("scorer prompt:", len(SYSP), "chars | Studio-governed:",
      ("GOVERNING ENGINE — Zycus Win Position" in SYSP), flush=True)

# label, opp_id (15-char for deal_records lookup)
OPPS = [
    ("ACEN (anchor)",     "006P700000DkWgX"),
    ("Allstate",          "006P7000006uKrq"),
    ("Cebu Pacific Air",  "0066700000wdNe1"),
    ("Publicis Groupe",   "006P700000Xl06R"),
    ("John Deere",        "006P700000KHd9V"),
    ("Telcel",            "006P700000aBK6l"),
    ("GSK plc",           "006P700000aZ93k"),
    ("Saudia Airlines",   "006P700000aEeX8"),
]

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dryrun_local")
os.makedirs(OUT, exist_ok=True)


def get_record(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "record", "opp_id": f"eq.{oid}"},
                     headers=H, verify=VERIFY, timeout=60).json()
    return r[0]["record"] if isinstance(r, list) and r else None


def anthropic_score(packet):
    user = ("Score this opportunity. Evidence packet (facts only):\n\n"
            + json.dumps(packet, default=str, ensure_ascii=False))
    for attempt in range(5):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": AK, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": "claude-sonnet-5", "max_tokens": 16000, "system": SYSP,
                                    "messages": [{"role": "user", "content": user}]},
                              verify=False, timeout=600)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(20 * (attempt + 1))
                continue
            r.raise_for_status()
            out = r.json()
            text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
            return text, out.get("stop_reason")
        except requests.RequestException:
            time.sleep(15 * (attempt + 1))
    return "", "error"


def score_one(label, oid):
    try:
        rec = get_record(oid)
        if not rec:
            return {"label": label, "opp_id": oid, "status": "NO RECORD (needs sweep)"}
        packet = EV.build_evidence_packet(rec)
        text, stop = anthropic_score(packet)
        parsed = _extract_json(text)
        if not (isinstance(parsed, dict) and parsed.get("scores")):
            return {"label": label, "opp_id": oid,
                    "status": f"unusable (stop={stop}, chars={len(text)})"}
        ds = A._normalize(parsed, packet)
        hl = ds["headline"]
        return {"label": label, "opp_id": oid, "status": "ok",
                "win": hl["win_position"], "mom": hl["deal_momentum"], "read": hl["read"],
                "commit": hl["customer_commitment"], "risk": hl["deal_risk"],
                "fc": hl["forecast_confidence"], "reasons": ds.get("ai_reasons") or {},
                "deal_scores": ds,
                "calls_read": ((rec.get("evidence_coverage") or {}).get("calls_read")),
                "packet_chars": len(json.dumps(packet, default=str))}
    except Exception as e:
        return {"label": label, "opp_id": oid, "status": f"ERROR {type(e).__name__}: {e}"}


results = []
with cf.ThreadPoolExecutor(max_workers=4) as ex:
    futs = {ex.submit(score_one, l, o): (l, o) for l, o in OPPS}
    for fut in cf.as_completed(futs):
        res = fut.result()
        results.append(res)
        l, oid = res["label"], res["opp_id"]
        if res["status"] == "ok":
            print(f"\n===== {l} ({oid}) — WIN {res['win']} | MOM {res['mom']} | read={res['read']} "
                  f"| commit={res['commit']} risk={res['risk']} fc={res['fc']} "
                  f"| calls_read={res['calls_read']} | packet={res['packet_chars']}c =====", flush=True)
            for key in ("win_position", "deal_momentum"):
                bullets = res["reasons"].get(key) or []
                print(f"  -- {key} ({len(bullets)} reasons) --", flush=True)
                for b in bullets:
                    tone = b.get("tone") if isinstance(b, dict) else "?"
                    txt = b.get("text") if isinstance(b, dict) else str(b)
                    print(f"    [{tone}] {txt}", flush=True)
            with open(os.path.join(OUT, f"{oid}.json"), "w", encoding="utf-8") as f:
                json.dump(res["deal_scores"], f, indent=1, default=str)
        else:
            print(f"\n===== {l} ({oid}) — {res['status']} =====", flush=True)

order = {o: i for i, (_, o) in enumerate(OPPS)}
results.sort(key=lambda r: order.get(r["opp_id"], 99))
CSV = os.path.join(OUT, "_scores.csv")
with open(CSV, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["label", "opp_id", "status", "win", "mom", "read", "commit", "risk",
                "forecast_conf", "calls_read", "packet_chars", "win_reasons_n", "mom_reasons_n"])
    for r in results:
        rs = r.get("reasons") or {}
        w.writerow([r.get("label"), r.get("opp_id"), r.get("status"), r.get("win"), r.get("mom"),
                    r.get("read"), r.get("commit"), r.get("risk"), r.get("fc"), r.get("calls_read"),
                    r.get("packet_chars"),
                    len(rs.get("win_position") or []), len(rs.get("deal_momentum") or [])])
print(f"\n[DONE] CSV: {CSV}  |  per-deal reasons JSON under {OUT}/", flush=True)
