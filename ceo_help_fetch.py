"""CEO-help v2 FETCH: build a VERIFIED evidence pack per gate-passer —
stored analysis + Avoma transcript evidence + our-side engagement (is_rep) +
Apollo-verified buyer C-suite. All local, read-only. Output: ceo_evidence_v2.json
"""
import json, re, sys, requests, urllib3
from daily_summary.common import load_secret, load_datalake, datalake_get
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret()
DL = load_datalake()
BASE = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
APOLLO = sec.get("APOLLO_API_KEY")
SBH = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
ROOM = re.compile(r"besprechungsraum|meeting room|\bpar\b|\buz\b|conference", re.I)
TERMS = ["blocker", "block", "price", "pricing", "discount", "budget", "timeline", "delay",
         "next year", "Q1", "Q3", "Q4", "decision", "sign", "competitor", "escalat",
         "resource", "POC", "integration", "SAP", "board", "CFO", "CEO", "CIO"]
EXEC_TITLES = ["Chief Financial Officer", "CFO", "Chief Information Officer", "CIO",
               "Chief Procurement Officer", "CPO", "Chief Executive Officer", "CEO",
               "Chief Operating Officer", "COO", "Chief Technology Officer", "CTO",
               "VP Procurement", "VP Finance", "Head of Procurement", "Head of IT"]


def sb(q):
    return requests.get(f"{BASE}/rest/v1/{q}", headers=SBH, verify=False, timeout=60).json()


import time

KNOWN_ZYCUS = {"amit shah": "Chief Marketing Officer (CMO)", "john woodcock": "VP / SVP Sales"}


def apollo(path, body):
    for attempt in range(2):
        try:
            r = requests.post(f"https://api.apollo.io/v1/{path}",
                              headers={"Content-Type": "application/json", "X-Api-Key": APOLLO},
                              json=body, verify=False, timeout=40)
            if r.status_code == 429:
                time.sleep(2.5); continue
            return r.json() if r.status_code < 300 else {}
        except Exception:
            time.sleep(1.5)
    return {}


def _clean(name):
    return re.sub(r"\b(inc|llc|ltd|gmbh|corporation|corp|co|ag|plc|s\.?a|group|limited|the)\b\.?",
                  " ", name or "", flags=re.I).strip(" ,.()")


def domain_for(name):
    q = _clean(name)
    toks = [t for t in re.findall(r"[a-z]{3,}", q.lower()) if t not in ("the", "and")]
    d = apollo("mixed_companies/search", {"q_organization_name": q, "per_page": 1})
    orgs = d.get("organizations") or d.get("accounts") or []
    if not orgs:
        return ""
    o = orgs[0]
    dom = (o.get("primary_domain") or "").lower()
    onm = (o.get("name") or "").lower()
    reg = dom.split(".")[0] if dom else ""
    # accept Apollo's top hit only if it plausibly matches (else skip → judge marks unverified)
    ok = (toks and toks[0] in onm) or any(t == reg for t in toks) or any(len(t) >= 5 and t in dom for t in toks)
    return dom if ok else ""


def execs_for(domain):
    if not domain:
        return []
    d = apollo("mixed_people/search", {"q_organization_domains": domain,
               "person_titles": EXEC_TITLES, "person_seniorities": ["c_suite", "vp", "head", "director"],
               "per_page": 20, "page": 1})
    out = []
    for p in (d.get("people") or []):
        out.append({"name": p.get("name"), "title": p.get("title")})
    return out


def transcripts(opp15):
    ms = datalake_get(DL, f"avoma_meetings?crm_opportunity_id=ilike.{opp15}*&select=uuid,subject,start_at&order=start_at.desc&limit=40") or []
    corpus, zycus, buyer = "", {}, {}
    last = ms[0]["start_at"][:10] if ms else None
    for m in ms[:18]:
        tr = datalake_get(DL, f"avoma_transcripts?meeting_uuid=eq.{m['uuid']}&select=transcript_text,speakers&limit=1") or []
        if not tr:
            continue
        corpus += "\n" + (tr[0].get("transcript_text") or "")
        spk = tr[0].get("speakers")
        if isinstance(spk, str):
            try:
                spk = json.loads(spk)
            except Exception:
                spk = []
        for s in (spk or []):
            if not isinstance(s, dict):
                continue
            nm = (s.get("name") or "").strip()
            if not nm or ROOM.search(nm):
                continue
            (zycus if s.get("is_rep") else buyer)[nm] = (zycus if s.get("is_rep") else buyer).get(nm, 0) + 1
    return corpus, len(ms), last, zycus, buyer


def excerpts(corpus):
    out, seen = [], set()
    for t in TERMS:
        mm = re.search(re.escape(t), corpus, re.I)
        if mm:
            i = mm.start() // 500
            if (t.lower(), i) in seen:
                continue
            seen.add((t.lower(), i))
            s = corpus[max(0, mm.start() - 130):mm.start() + 170].replace("\n", " ").strip()
            out.append(f"[{t}] …{s}…")
        if len(out) >= 8:
            break
    return out


def surname_match(exec_name, roster_names):
    parts = [p for p in re.split(r"\s+", (exec_name or "")) if len(p) > 2]
    for r in roster_names:
        rl = r.lower()
        if any(p.lower() in rl for p in parts):
            return True
    return False


def main():
    passers = json.load(open("ceo_passers.json", encoding="utf-8"))
    zycus_roster = execs_for("zycus.com")  # who ARE the Zycus execs

    def zycus_title(name):
        for z in zycus_roster:
            if surname_match(name, [z["name"]]):
                return z["title"]
        return KNOWN_ZYCUS.get((name or "").strip().lower())
    print(f"Zycus exec roster (Apollo): {len(zycus_roster)}")
    packs = []
    for p in passers:
        opp = p["opp_id"]
        rec = (sb(f"deal_records?opp_id=eq.{opp}&select=record") or [{}])[0].get("record") or {}
        ai, hard = rec.get("ai") or {}, rec.get("hard") or {}
        corpus, ntr, last, zspk, bspk = transcripts(opp)
        dom = domain_for(p["account"])
        bexecs = execs_for(dom)
        for e in bexecs:
            e["engaged"] = surname_match(e["name"], list(bspk))
        zengaged = [{"name": n, "title": zycus_title(n)} for n in zspk]
        pack = {
            "opp_id": opp, "account": p["account"], "owner": p["owner"], "amount": p["amount"],
            "stage": p["stage"], "win": p["win"], "mom": p["mom"], "domain": dom,
            "close_date": hard.get("close_date"),
            "competitive_position": (ai.get("competitive_position") or {}).get("summary"),
            "champion": ai.get("champion_strength") or {},
            "vulnerabilities": ai.get("vulnerabilities") or ai.get("gaps"),
            "recommended_moves": [m.get("action") for m in ((ai.get("recommended_moves") or {}).get("items") or [])[:3]],
            "next_step": hard.get("next_step") or (ai.get("north_star_verdict") or {}).get("read"),
            "transcript_calls": ntr, "last_call": last,
            "transcript_evidence": excerpts(corpus) if corpus else [],
            "zycus_execs_engaged": zengaged,
            "buyer_people_on_calls": list(bspk),
            "buyer_execs_verified": bexecs,
        }
        packs.append(pack)
        eng_exec = [f"{z['name']} ({z['title']})" for z in zengaged if z.get("title")]
        print(f"  {opp} | {p['account'][:22].ljust(22)} | dom={(dom or '?'):22} | calls={ntr:2} | "
              f"buyer_execs={len(bexecs)} | zycus_exec_engaged={eng_exec or '-'}")
    json.dump(packs, open("ceo_evidence_v2.json", "w", encoding="utf-8"), indent=2, default=str)
    print(f"\nwrote ceo_evidence_v2.json ({len(packs)} packs)")


if __name__ == "__main__":
    main()
