"""CEO-help v3 FETCH — SFDC-first, NO Apollo. Per gate-passer, build an evidence
pack for a CEO-only judge: strategic context (stored record) + buyer DECISION
AUTHORITY from Salesforce OpportunityContactRole + engagement from SFDC activities
& Avoma + our-side execs already engaged + transcript proof. Read-only."""
import json, re, sys, unicodedata
from daily_summary.common import (load_secret, sf_login, soql, load_datalake,
                                  datalake_get, id15)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret()
DL = load_datalake()
C_LEVEL = ("chief", "cfo", "ceo", "coo", "cto", "cio", "cpo", "cmo", "cro",
           "managing director", "generaldirektor", "geschäftsführer", "president", "vorstand")
FIN = ("cfo", "chief financial", "finance", "financ", "finanz", "controller", "treasur", "budget")
VP_HEAD = ("svp", "evp", "vp ", " vp", "vice president", "head of", "leiter")
DEC_ROLES = {"economic buyer", "decision maker", "decision-maker"}
TERMS = ["blocker", "block", "price", "pricing", "discount", "budget", "commercial",
         "timeline", "delay", "next year", "decision", "sign", "competitor", "escalat",
         "resource", "POC", "integration", "board", "roadmap", "feature", "capability"]


def _n(r, *path):
    cur = r
    for p in path[:-1]:
        cur = (cur or {}).get(p) or {}
    return (cur or {}).get(path[-1])


def fold(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    s = re.sub(r"[^a-z ]", " ", s.lower())
    return {t for t in s.split() if len(t) >= 3}


def match(a, names):
    ta = fold(a)
    return any(ta and (ta <= fold(b) or fold(b) <= ta) for b in names)


def authority(title, role):
    t = (title or "").lower(); r = (role or "").lower()
    if r in DEC_ROLES or any(w in t for w in C_LEVEL):
        return "decision"
    if any(w in t for w in FIN):
        return "finance"
    if any(w in t for w in VP_HEAD):
        return "senior"
    return None


def sb(path):
    import requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    base = sec["SUPABASE_URL"].rstrip("/"); key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    return requests.get(f"{base}/rest/v1/{path}", headers={"apikey": key, "Authorization": f"Bearer {key}"}, verify=False, timeout=60).json()


def transcripts(opp15):
    ms = datalake_get(DL, f"avoma_meetings?crm_opportunity_id=ilike.{opp15}*&select=uuid,subject,start_at&order=start_at.desc&limit=30") or []
    corpus, buyer, zycus = "", set(), {}
    for m in ms[:12]:
        tr = datalake_get(DL, f"avoma_transcripts?meeting_uuid=eq.{m['uuid']}&select=transcript_text,speakers&limit=1") or []
        if not tr:
            continue
        corpus += "\n" + (tr[0].get("transcript_text") or "")
        spk = tr[0].get("speakers")
        if isinstance(spk, str):
            try: spk = json.loads(spk)
            except Exception: spk = []
        for s in (spk or []):
            if isinstance(s, dict) and s.get("name") and not re.search(r"besprechungsraum|\bpar\b|\buz\b|room", s["name"], re.I):
                if s.get("is_rep"):
                    zycus.setdefault(s["name"], 1)
                else:
                    buyer.add(s["name"])
    return corpus, len(ms), list(buyer), list(zycus)


def excerpts(corpus):
    out, seen = [], set()
    for t in TERMS:
        mm = re.search(re.escape(t), corpus, re.I)
        if mm and (t.lower()) not in seen:
            seen.add(t.lower())
            s = corpus[max(0, mm.start() - 120):mm.start() + 160].replace("\n", " ").strip()
            out.append(f"[{t}] …{s}…")
        if len(out) >= 8:
            break
    return out


KNOWN_ZYCUS = {"amit shah": "Chief Marketing Officer (CMO)", "john woodcock": "VP / SVP Sales"}


def main():
    passers = json.load(open("ceo_passers.json", encoding="utf-8"))
    sid, inst = sf_login(sec)
    packs = []
    for p in passers:
        opp = p["opp_id"]
        rec = (sb(f"deal_records?opp_id=eq.{opp}&select=record") or [{}])[0].get("record") or {}
        ai, hard = rec.get("ai") or {}, rec.get("hard") or {}

        # buyer authority from SFDC OpportunityContactRole
        roles = soql(sid, inst, f"SELECT Contact.Name, Contact.Title, Role FROM OpportunityContactRole WHERE OpportunityId='{opp}'")
        # engagement sources: SFDC task contacts + Avoma buyer attendees
        tks = soql(sid, inst, f"SELECT Who.Name FROM Task WHERE WhatId='{opp}' AND WhoId!=null ORDER BY ActivityDate DESC NULLS LAST LIMIT 60")
        sf_engaged = [_n(t, "Who", "Name") for t in tks if _n(t, "Who", "Name")]
        corpus, ncalls, buyer_on_calls, zycus_spk = transcripts(id15(opp))
        engaged_names = sf_engaged + buyer_on_calls

        buyer_authority = []
        for r in roles:
            nm, ti, ro = _n(r, "Contact", "Name"), _n(r, "Contact", "Title"), r.get("Role")
            lvl = authority(ti, ro)
            if lvl:
                buyer_authority.append({"name": nm, "title": ti, "role": ro,
                                        "level": lvl, "engaged": match(nm, engaged_names)})
        # dedupe authority by name
        seen = set(); buyer_authority = [a for a in buyer_authority if not (fold(a["name"]) and tuple(sorted(fold(a["name"]))) in seen or seen.add(tuple(sorted(fold(a["name"])))))]

        zengaged = [{"name": n, "title": KNOWN_ZYCUS.get(n.lower())} for n in zycus_spk]

        pack = {
            "opp_id": opp, "account": p["account"], "owner": p["owner"], "amount": p["amount"],
            "stage": p["stage"], "win": p["win"], "mom": p["mom"], "close_date": hard.get("close_date"),
            "competitive_position": (ai.get("competitive_position") or {}).get("summary"),
            "champion": ai.get("champion_strength") or {},
            "gaps": ai.get("gaps") or ai.get("vulnerabilities"),
            "recommended_moves": [m.get("action") for m in ((ai.get("recommended_moves") or {}).get("items") or [])[:3]],
            "next_step": hard.get("next_step"),
            "meddpicc_economic_buyer": (ai.get("meddpicc") or {}).get("economic_buyer") or (ai.get("meddpicc") or {}).get("economic_buyer_status"),
            "buyer_decision_authority_SFDC": buyer_authority,  # who can say yes + engaged?
            "buyer_people_on_calls": buyer_on_calls,
            "zycus_execs_engaged": zengaged,   # lower execs already in (CEO context)
            "transcript_calls": ncalls,
            "transcript_evidence": excerpts(corpus) if corpus else [],
        }
        packs.append(pack)
        json.dump(pack, open(f"ceo_evidence/{opp}.json", "w", encoding="utf-8"), indent=2, default=str)
        auth = [f"{a['name']}({a['title']},{'✓' if a['engaged'] else '✗'})" for a in buyer_authority[:4]]
        print(f"  {opp} | {p['account'][:20].ljust(20)} | calls={ncalls:2} | authority={auth or '—'} | zycus_exec={[z['name'] for z in zengaged] or '—'}")
    print(f"\nwrote {len(packs)} SFDC-only evidence packs (no Apollo)")


if __name__ == "__main__":
    main()
