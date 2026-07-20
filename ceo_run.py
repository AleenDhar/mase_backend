# -*- coding: utf-8 -*-
"""Run ONLY the CEO logic for the whole book, using the GOVERNED Omnivision `ceo`
engine (scoring_instructions, engine='ceo', locked). This WIRES the engine to real
use: it loads the locked ceo prompt, runs the SUPPORT/WATCH determination per deal via
the LLM, applies the deterministic floor/clamp/recency (faithful to deal_engine_ceo),
and writes ai.ceo_intervention back — nothing else on the record is touched.

Eligible (win>=40) deals get the LLM determination at high concurrency (small packs ->
direct to Anthropic, no ECS). Below-40 deals get the deterministic floor (carry prior
watches within 90d, else not-needed) with no LLM call. Usage: python ceo_run.py [--conc 32]
"""
import sys, json, re, time, warnings, datetime
from concurrent.futures import ThreadPoolExecutor
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CONC = int(sys.argv[sys.argv.index("--conc") + 1]) if "--conc" in sys.argv else 32
MODEL = "claude-sonnet-5"
WIN_BAR = 40.0
LEVERS = {"pricing", "product", "presales_resources", "exec_connect"}
TODAY = datetime.date(2026, 7, 14).isoformat()   # stamped (Date.now unavailable in-tool; real run date)

cfg = {}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    l = l.strip()
    if l and "=" in l and not l.startswith("#"):
        k, v = l.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
B = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": K, "Authorization": "Bearer " + K}
AK = json.load(open(".mase_app_env.json"))["ANTHROPIC_API_KEY"]
sec = {}
for l in open(".supabase_secrets.env", encoding="latin-1"):
    l = l.strip()
    if l and "=" in l and not l.startswith("#"):
        k, v = l.split("=", 1); sec[k.strip()] = v.strip().strip('"').strip("'")
MGMT = sec["SUPABASE_ACCESS_TOKEN"]; REF = cfg["NEXT_PUBLIC_SUPABASE_URL"].split("//")[1].split(".")[0]


def num(v):
    try:
        return float(v)
    except Exception:
        return None


def within(as_of, days):
    if not as_of:
        return True   # legacy / undated -> keep (can't prove stale)
    try:
        d = datetime.date.fromisoformat(str(as_of)[:10])
        return (datetime.date.fromisoformat(TODAY) - d).days <= days
    except Exception:
        return True


CEO_PROMPT = requests.get(B + "/rest/v1/scoring_instructions",
                          params={"select": "content,version", "engine": "eq.ceo", "locked": "is.true"},
                          headers=H, verify=False, timeout=60).json()[0]["content"]

SEL = ("opp_id,hard:record->hard,scores:record->ai->deal_scores->headline,"
       "medd:record->ai->meddpicc,champ:record->ai->champion_strength,"
       "comp:record->ai->competitive_position,dm:record->ai->deal_movement,"
       "moves:record->ai->recommended_moves,er:record->ai->explicit_requirements,"
       "ir:record->ai->implicit_requirements,sc:record->ai->scope_change,"
       "ds:record->ai->day_summary,vuln:record->ai->vulnerabilities,"
       "prior:record->ai->ceo_intervention")


def load_all():
    out, off = [], 0
    while True:
        r = requests.get(B + "/rest/v1/deal_records",
                         params={"select": SEL, "active": "is.true", "order": "opp_id.asc",
                                 "limit": "200", "offset": str(off)},
                         headers=H, verify=False, timeout=120).json()
        if not r:
            break
        out.extend(r); off += 200
        if len(r) < 200:
            break
    return out


def pack_for(rec):
    hard = rec.get("hard") or {}
    hl = rec.get("scores") or {}
    medd = rec.get("medd") or {}
    p = {
        "opp_id": rec.get("opp_id"), "today": TODAY,
        "account": hard.get("account_name"), "opp_name": hard.get("opp_name"),
        "owner_rsd": hard.get("owner_name"),
        "vp": hard.get("manager_name"),   # escalation target — the CEO asks the VP, not the rep
        "amount": hard.get("amount"),
        "is_large": (num(hard.get("amount")) or 0) >= 250000,
        "forecast_category": hard.get("forecast_category"), "stage": hard.get("stage"),
        "close_date": hard.get("close_date"), "days_to_close": hard.get("days_to_close"),
        "last_activity_date": hard.get("last_activity_date"),
        "win_position": hl.get("win_position"), "deal_momentum": hl.get("deal_momentum"),
        "read": hl.get("read"),
        "economic_buyer": (medd.get("economic_buyer") or {}) if isinstance(medd, dict) else {},
        "champion": (medd.get("champion") or {}) if isinstance(medd, dict) else {},
        "champion_strength": rec.get("champ"),
        "competitive_position": rec.get("comp"),
        "recent_deal_movement": rec.get("dm"),
        "our_recommended_moves": rec.get("moves"),
        "explicit_requirements": rec.get("er"),
        "our_open_deliverables": rec.get("ir"),
        "scope_change": rec.get("sc"),
        "day_summary": rec.get("ds"),
        "vulnerabilities": (((rec.get("vuln") or {}).get("items") or [])[:4]) if isinstance(rec.get("vuln"), dict) else rec.get("vuln"),
        "prior_ceo_intervention": rec.get("prior"),
    }
    blob = json.dumps(p, default=str)
    return blob[:24000]   # cap the pack


def extract_json(txt):
    """Balanced-brace extraction — tolerant of code fences, prose, and truncation
    (a truncated object never closes -> returns {} rather than crashing)."""
    if not txt:
        return {}
    i = txt.find("{")
    if i < 0:
        return {}
    depth = 0; instr = False; esc = False
    for j in range(i, len(txt)):
        c = txt[j]
        if esc:
            esc = False; continue
        if c == "\\":
            esc = True; continue
        if c == '"':
            instr = not instr; continue
        if instr:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(txt[i:j + 1])
                except Exception:
                    return {}
    return {}


def call_llm(blob):
    body = {"model": MODEL, "max_tokens": 4000, "system": CEO_PROMPT,
            "messages": [{"role": "user", "content": "Decide CEO ATTENTION for this deal per your system instruction. Deal pack (JSON):\n\n" + blob}]}
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": AK, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json=body, verify=False, timeout=(10, 150))
    if r.status_code >= 300:
        raise RuntimeError(f"anthropic {r.status_code}: {r.text[:200]}")
    txt = ""
    for blk in r.json().get("content", []):
        if blk.get("type") == "text":
            txt += blk.get("text", "")
    return extract_json(txt)


def to_vp(ask, rep, vp):
    """Deterministically GUARANTEE the ceo_ask names the actual VP (never the rep,
    never the generic 'the deal owner's VP'):
      1) VP already named up front -> leave it.
      2) generic 'the deal owner's VP/manager' -> substitute the real VP name.
      3) opens 'Ask <rep>' -> swap the rep's name for the VP's.
    The CEO asks the VP by name; the VP works the rep."""
    if not ask or not vp:
        return ask
    vpf = vp.split()[0] if vp.split() else vp
    if vpf.lower() in ask[:55].lower():
        return ask
    generic = re.compile(r"the deal owner'?s (?:VP|manager)", re.I)
    if generic.search(ask):
        return generic.sub(vp, ask, count=1)
    if rep:
        for nm in ([rep, rep.split()[0]] if rep.split() else [rep]):
            if not nm:
                continue
            m = re.match(r"^(\s*ask\s+)" + re.escape(nm) + r"\b", ask, re.I)
            if m:
                return ask[:m.end(1)] + vp + ask[m.end():]
    return ask


def build_ceo(llm_out, rec, win, mom):
    hard = rec.get("hard") or {}
    prior = rec.get("prior") if isinstance(rec.get("prior"), dict) else {}
    eligible = win is not None and win >= WIN_BAR
    reasons = []
    sup = (llm_out or {}).get("support") or {}
    if eligible and sup.get("needed") is True:
        areas = [a for a in (sup.get("areas") or []) if a in LEVERS] or ["exec_connect"]
        pr = sup.get("priority") if sup.get("priority") in ("high", "medium") else \
            ("high" if (num(hard.get("amount")) or 0) > 400000 else "medium")
        ev = sup.get("evidence")
        reasons.append({"type": "support", "act": True, "severity": pr, "areas": areas,
                        "summary": sup.get("summary") or sup.get("detail"), "detail": sup.get("detail"),
                        "metric": sup.get("metric"), "owner": sup.get("owner"),
                        "vp": sup.get("vp") or hard.get("manager_name"),
                        "ceo_action": sup.get("ceo_action"),
                        "ceo_ask": to_vp(sup.get("ceo_ask"), hard.get("owner_name"), hard.get("manager_name")),
                        "buyer_target": sup.get("buyer_target") or {}, "why_not_vp": sup.get("why_not_vp"),
                        "evidence": ev if isinstance(ev, list) else ([ev] if ev else []), "as_of": TODAY})
    mon = (llm_out or {}).get("monitor") or {}
    if eligible:
        for t in (mon.get("triggers") or []):
            if isinstance(t, dict) and t.get("type") in ("our_slip", "large_slowdown", "competitor_edge") and within(t.get("as_of"), 14):
                reasons.append({"type": t["type"], "act": False,
                                "severity": t.get("severity") if t.get("severity") in ("high", "medium") else "medium",
                                "summary": t.get("summary"), "detail": t.get("detail"), "metric": t.get("metric"),
                                "owner": t.get("owner"), "vp": t.get("vp") or hard.get("manager_name"),
                                "ceo_ask": to_vp(t.get("ceo_ask"), hard.get("owner_name"), hard.get("manager_name")),
                                "evidence": t.get("evidence"), "as_of": t.get("as_of")})
    # native scope_shrink + carried-prior watches are CEO supervision — only for
    # eligible (win>=40) deals, so the CEO column is a tight watchlist and not
    # cluttered with months-old scope shrinks on dead/low-win deals.
    if eligible:
        sc = rec.get("sc") if isinstance(rec.get("sc"), dict) else {}
        if str(sc.get("direction") or "").strip().lower() in ("reduced", "reduced_scope", "shrunk", "shrinking", "narrowed", "narrowing", "down"):
            amt = num(hard.get("amount")) or 0.0
            reasons.append({"type": "scope_shrink", "act": False, "severity": "high" if amt >= 250000 else "medium",
                            "summary": "Scope shrinking vs prior — " + str(sc.get("detail") or sc.get("to") or "narrower scope than before")[:160],
                            "detail": sc.get("detail"), "as_of": TODAY})
        have = set(r["type"] for r in reasons)
        for r in (prior.get("reasons") or []):
            if isinstance(r, dict) and r.get("type") != "support" and r.get("type") not in have and within(r.get("as_of"), 90):
                reasons.append(r); have.add(r.get("type"))
    seen, dedup = set(), []
    for r in reasons:
        t = r.get("type")
        if (t == "support" or t not in seen) and within(r.get("as_of"), 90):
            dedup.append(r); seen.add(t)
    needed = bool(dedup)
    sev = "high" if any(r.get("severity") == "high" for r in dedup) else "medium"
    summ = ""
    if dedup:
        top = sorted(dedup, key=lambda r: (r.get("type") == "support", r.get("severity") == "high"), reverse=True)[0]
        summ = (top.get("summary") or top.get("detail") or "")[:220]
    return {"needed": needed, "severity": sev if needed else None,
            "needs_action": any(r.get("type") == "support" for r in dedup),
            "summary": summ, "reasons": dedup, "win": win, "mom": mom,
            "source": "ceo_v1", "generated_at": TODAY}


RESULTS = {}
ERR = {"n": 0}


def work(rec):
    oid = rec.get("opp_id")
    win = num((rec.get("scores") or {}).get("win_position"))
    mom = num((rec.get("scores") or {}).get("deal_momentum"))
    llm = None
    if win is not None and win >= WIN_BAR:
        try:
            llm = call_llm(pack_for(rec))
        except Exception as e:  # noqa: BLE001
            ERR["n"] += 1
            print(f"  LLM fail {oid}: {type(e).__name__}: {str(e)[:80]}", flush=True)
            llm = None
    RESULTS[oid] = build_ceo(llm, rec, win, mom)


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


recs = load_all()
elig = [r for r in recs if (num((r.get("scores") or {}).get("win_position")) or 0) >= WIN_BAR]
print(f"[{ts()}] loaded {len(recs)} active | eligible(win>=40)={len(elig)} -> LLM | rest deterministic | conc={CONC} | ceo engine loaded ({len(CEO_PROMPT)} chars)", flush=True)

with ThreadPoolExecutor(max_workers=CONC) as ex:
    list(ex.map(work, recs))

print(f"[{ts()}] computed {len(RESULTS)} | LLM errors={ERR['n']}", flush=True)

# write ai.ceo_intervention back via mgmt SQL jsonb_set (dollar-quoted, chunked)
items = list(RESULTS.items())
wrote = 0
for i in range(0, len(items), 40):
    chunk = items[i:i + 40]
    stmts = []
    for oid, ci in chunk:
        js = json.dumps(ci, default=str).replace("$ceo$", "")
        stmts.append(f"UPDATE deal_records SET record=jsonb_set(record,'{{ai,ceo_intervention}}',$ceo${js}$ceo$::jsonb,true), updated_at=now() WHERE opp_id='{oid}';")
    q = "\n".join(stmts)
    r = requests.post(f"https://api.supabase.com/v1/projects/{REF}/database/query",
                      headers={"Authorization": "Bearer " + MGMT, "Content-Type": "application/json"},
                      json={"query": q}, verify=False, timeout=120)
    if r.status_code < 300:
        wrote += len(chunk)
    else:
        print(f"  write chunk {i} FAILED HTTP {r.status_code}: {r.text[:200]}", flush=True)
    print(f"[{ts()}] wrote {wrote}/{len(items)}", flush=True)

needed = [ci for ci in RESULTS.values() if ci["needed"]]
support = [ci for ci in needed if ci["needs_action"]]
watch = [ci for ci in needed if not ci["needs_action"]]
print(f"\n===== CEO RUN DONE ({len(RESULTS)} deals) =====")
print(f"  needed=true: {len(needed)}  (support/act: {len(support)} | watch-only: {len(watch)})")
print("  top CEO-attention deals:")
for oid, ci in sorted(RESULTS.items(), key=lambda kv: (kv[1]["needs_action"], kv[1]["severity"] == "high"), reverse=True)[:12]:
    if ci["needed"]:
        types = ",".join(sorted(set(r.get("type") for r in ci["reasons"])))
        print(f"    {oid} win={ci['win']} sev={ci['severity']} act={ci['needs_action']} [{types}] {str(ci['summary'])[:70]}")
print("CEO-RUN-DONE")
