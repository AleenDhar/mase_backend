"""Authoritative roster of who was on the Austrian Post calls: diarized speakers
(with is_rep) + attendee emails by domain. Read-only."""
import json, sys, re
from daily_summary.common import load_datalake, datalake_get
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OID = "006P700000J71MD"
ROOM = re.compile(r"besprechungsraum|\braum\b|meeting room|\bpar\b|\buz\b", re.I)


def speakers_raw(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return []
    return v if isinstance(v, list) else []


def as_emails(v):
    if not v:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            v = re.split(r"[,;]", v)
    out = []
    for x in (v if isinstance(v, list) else [v]):
        s = (x.get("email") if isinstance(x, dict) else str(x)).strip().lower()
        if "@" in s:
            out.append(s)
    return out


def main():
    dl = load_datalake()
    ms = datalake_get(dl, f"avoma_meetings?crm_opportunity_id=ilike.{OID}*&select=uuid,subject,start_at,attendee_emails&order=start_at.desc&limit=60") or []

    zycus_reps, buyer_people, rooms = {}, {}, {}
    emails = {}
    calls_with_transcript = 0
    for m in ms:
        for e in as_emails(m.get("attendee_emails")):
            emails[e] = emails.get(e, 0) + 1
        tr = datalake_get(dl, f"avoma_transcripts?meeting_uuid=eq.{m['uuid']}&select=speakers&limit=1") or []
        if not tr:
            continue
        raw = speakers_raw(tr[0].get("speakers"))
        if raw:
            calls_with_transcript += 1
        for sp in raw:
            if not isinstance(sp, dict):
                continue
            nm = (sp.get("name") or "").strip()
            if not nm:
                continue
            if ROOM.search(nm):
                rooms[nm] = rooms.get(nm, 0) + 1
            elif sp.get("is_rep"):
                zycus_reps[nm] = zycus_reps.get(nm, 0) + 1
            else:
                buyer_people[nm] = buyer_people.get(nm, 0) + 1

    def dump(title, d):
        print(f"\n== {title} ({len(d)}) ==")
        for nm, c in sorted(d.items(), key=lambda x: -x[1]):
            print(f"   {c:2}×  {nm}")

    print(f"Austrian Post calls: {len(ms)} ({calls_with_transcript} with a transcript)")
    dump("ZYCUS side — spoke on calls (is_rep=true)", zycus_reps)
    dump("AUSTRIAN POST side — spoke on calls (is_rep=false)", buyer_people)
    dump("meeting-room / device mics (not people)", rooms)

    print("\n== attendee emails by domain ==")
    dom = {}
    for e, c in emails.items():
        d = e.split("@")[1]
        dom.setdefault(d, []).append((e, c))
    for d in sorted(dom, key=lambda d: -len(dom[d])):
        print(f"  @{d}:")
        for e, c in sorted(dom[d], key=lambda x: -x[1]):
            print(f"     {c:2}×  {e}")

    blob = " ".join(list(zycus_reps) + list(buyer_people) + list(emails)).lower()
    print("\n== presence of the 'CEO help' names ==")
    for who in ["flandorfer", "potisk", "eibensteiner", "oblin", "jettmar"]:
        print(f"   {who:14}: {'PRESENT on a call' if who in blob else 'NEVER on any call'}")


if __name__ == "__main__":
    main()
