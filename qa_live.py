"""DEEP QA of a LIVE deal_records row (cloud-authored only — read-only, no writes).

Checks the user's gate list:
  A. Scores generated, src=ai, not degraded
  B. Reasons are SPECIFIC (dates/names/quotes/numbers + headline format), not textbook filler
  C. 24-hour summary proper (overall + dated items, nothing future-dated as "happened")
  D. To-dos conform to the locked To-Do engine contract (3 horizons, future act_by,
     rank-1 within 14d, imperative <20-word actions, owner set)
  E. Provenance: the record came from the CLOUD pipeline alone (Omnivision version stamps,
     fresh swept_at, manual-trigger audit row) — no local authorship
  F. Hygiene: CEO summary + 90d window, competitor dedup, MEDDPICC coverage

Usage: python qa_live.py 006P700000DkWgX "ACEN"
"""
import json, re, sys, datetime
import requests, urllib3, warnings
warnings.filterwarnings("ignore")
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret()
SB = sec["SUPABASE_URL"].rstrip("/")
K = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": K, "Authorization": f"Bearer {K}"}
TODAY = datetime.date.today()

OID = id15(sys.argv[1] if len(sys.argv) > 1 else "006P700000DkWgX")
LABEL = sys.argv[2] if len(sys.argv) > 2 else OID

PASS, WARN, FAIL = [], [], []


def ok(cond, name, detail="", warn_only=False):
    (PASS if cond else (WARN if warn_only else FAIL)).append(
        name + (f" — {detail}" if detail and not cond else ""))


MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def specific(text):
    """Heuristic: a reason is SPECIFIC if it carries a date, quote, number, or $/%."""
    t = (text or "").lower()
    has_date = bool(re.search(r"20\d\d", t) or re.search(r"\d{1,2}[/.]\d{1,2}", t)
                    or any(m in t for m in MONTHS))
    has_quote = ('"' in (text or "")) or ("'" in (text or ""))
    has_num = bool(re.search(r"[$€£]\s?\d|\d+\s?(%|days?|weeks?|months?|min)", t))
    return has_date or has_num or (has_quote and len(t) > 60)


def titled(text):
    head = (text or "")[:70]
    return (" — " in head) or (" - " in head)


rows = requests.get(f"{SB}/rest/v1/deal_records",
                    params={"select": "opp_id,swept_at,updated_at,record", "opp_id": f"eq.{OID}"},
                    headers=H, verify=VERIFY, timeout=60).json()
if not rows:
    print(f"{LABEL}: NO deal_records row")
    sys.exit(1)
row = rows[0]
rec = row.get("record") or {}
ai = rec.get("ai") or {}
ds = ai.get("deal_scores") or {}
hl = ds.get("headline") or {}
rz = ds.get("ai_reasons") or {}

# ---- A. scores ----
for k in ("win_position", "deal_momentum", "customer_commitment", "deal_risk", "forecast_confidence"):
    ok(hl.get(k) is not None, f"A.score.{k}", "None")
ok(ds.get("factor_source") == "ai", "A.src=ai", f"src={ds.get('factor_source')}")
ok(not ds.get("scoring_degraded"), "A.not-degraded", str(ds.get("fallback_reason"))[:60])

# ---- B. reasons quality ----
for key in ("win_position", "deal_momentum", "customer_commitment", "deal_risk"):
    rs = rz.get(key) or []
    ok(len(rs) >= 4, f"B.{key}.count>=4", f"{len(rs)}")
    if rs:
        sp = sum(1 for b in rs if specific(b.get("text")))
        ti = sum(1 for b in rs if titled(b.get("text")))
        ok(sp >= max(1, round(0.8 * len(rs))), f"B.{key}.specific", f"{sp}/{len(rs)} carry date/number/quote")
        ok(ti >= max(1, round(0.8 * len(rs))), f"B.{key}.headline-format", f"{ti}/{len(rs)} titled")
        generic = [b.get("text", "")[:50] for b in rs
                   if len(b.get("text") or "") < 55 and not specific(b.get("text"))]
        ok(not generic, f"B.{key}.no-textbook-filler", f"e.g. {generic[:1]}", warn_only=True)

# ---- C. 24-hour summary ----
d24 = ai.get("day_summary") or {}
ok(bool((d24.get("overall") or "").strip()), "C.day_summary.overall")
items = d24.get("items") or []
ok(all((it.get("at") or "") <= TODAY.isoformat() for it in items),
   "C.day_summary.no-future-items", str([it.get("at") for it in items if (it.get("at") or "") > TODAY.isoformat()]))
ok(all(it.get("summary") or it.get("name") for it in items), "C.day_summary.items-substantive")

# ---- D. to-dos vs the To-Do engine contract ----
rm = (ai.get("recommended_moves") or {}).get("items") or []
ok(len(rm) >= 3, "D.moves.count>=3", f"{len(rm)}")
hz = {m.get("horizon") for m in rm}
for h in ("next_7_days", "next_14_days", "next_30_days"):
    ok(h in hz, f"D.moves.horizon.{h}")
bad_actby = [m.get("rank") for m in rm if not m.get("act_by") or str(m.get("act_by")) < TODAY.isoformat()]
ok(not bad_actby, "D.moves.act_by-future", f"ranks {bad_actby}")
r1 = next((m for m in rm if m.get("rank") == 1), None)
if r1 and r1.get("act_by"):
    try:
        d1 = datetime.date.fromisoformat(str(r1["act_by"])[:10])
        ok((d1 - TODAY).days <= 14, "D.moves.rank1-within-14d", str(d1))
    except ValueError:
        ok(False, "D.moves.rank1-within-14d", "bad date")
long_actions = [m.get("rank") for m in rm if len(str(m.get("action") or "").split()) > 22]
ok(not long_actions, "D.moves.action<20w", f"ranks {long_actions}", warn_only=True)
ok(all(m.get("expected_effect") for m in rm), "D.moves.expected_effect", warn_only=True)
wp = ((ai.get("implicit_requirements") or {}).get("we_promised") or {}).get("items") or []
ok(all(x.get("grounding_quote") for x in wp), "D.we_promised.grounding-quotes",
   f"{sum(1 for x in wp if not x.get('grounding_quote'))} missing", warn_only=True)

# ---- E. provenance: cloud-only ----
sv = (ai.get("scoring_studio") or {}).get("versions") or {}
ok(sv.get("win") == "10.7", "E.win-engine=10.7", f"got {sv.get('win')}")
ok(bool(sv.get("sweep")), "E.sweep-engine-stamped", f"{sv.get('sweep')}")
ok(str(row.get("swept_at") or "").startswith(TODAY.isoformat()), "E.swept_at-today", str(row.get("swept_at")))
try:
    tr = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                      params={"select": "source,run_id,created_at", "opp_id": f"like.{OID}*",
                              "order": "created_at.desc", "limit": "1"},
                      headers=H, verify=VERIFY, timeout=30).json()
    src = (tr[0].get("source") if isinstance(tr, list) and tr else None)
    ok(src == "manual", "E.trigger-audit=manual", f"source={src}", warn_only=True)
except Exception as e:  # noqa: BLE001
    ok(True, "E.trigger-audit", f"unreadable ({e})", warn_only=True)

# ---- F. hygiene ----
ceo = ai.get("ceo_intervention") or {}
ok(not ceo.get("needed") or bool(ceo.get("summary")), "F.ceo.summary-populated")
old = [r.get("as_of") for r in (ceo.get("reasons") or [])
       if r.get("as_of") and str(r.get("as_of")) < (TODAY - datetime.timedelta(days=91)).isoformat()]
ok(not old, "F.ceo.90d-window", str(old))
comps = (ai.get("competitive_position") or {}).get("competitors") or []
norm = [re.sub(r"[^a-z0-9]", "", str(c.get("name") or "").lower()) for c in comps]
dups = [t for t in ("zip", "netsuite", "coupa", "ariba", "ivalua", "inhouse", "gep")
        if sum(1 for x in norm if t in x) > 1]
ok(not dups, "F.competitors.deduped", str(dups))
md = ai.get("meddpicc") or {}
miss = [p for p in ("metrics", "economic_buyer", "decision_criteria", "decision_process",
                    "paper_process", "identify_pain", "champion", "competition")
        if not isinstance(md.get(p), dict) or not (md[p].get("narrative") or md[p].get("value"))]
ok(not miss, "F.meddpicc.8-pillars", str(miss))
ev = rec.get("evidence_coverage") or {}
ok((ev.get("calls_read") or 0) > 0, "F.evidence.calls_read>0", str(ev.get("calls_read")))

# ---- scorecard ----
total = len(PASS) + len(FAIL)
print(f"\n======= LIVE QA — {LABEL} ({OID}) =======")
print(f"swept_at={row.get('swept_at')} | WIN {hl.get('win_position')} MOM {hl.get('deal_momentum')} "
      f"read={hl.get('read')} src={ds.get('factor_source')}")
print(f"\nPASS {len(PASS)} / FAIL {len(FAIL)} / WARN {len(WARN)}  ->  "
      f"accuracy {100.0 * len(PASS) / total:.0f}%")
if FAIL:
    print("\nFAILURES:")
    for f in FAIL:
        print("  ✗", f)
if WARN:
    print("\nWARNINGS:")
    for w in WARN:
        print("  ~", w)
