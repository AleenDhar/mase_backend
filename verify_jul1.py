"""Read yesterday's (2026-07-01) Austrian Post onsite call transcripts and surface
what happened + the core issue. Read-only."""
import re, sys
from daily_summary.common import load_datalake, datalake_get
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OID = "006P700000J71MD"
TERMS = ["170", "SAP", "HANA", "Tronkeon", "eskal", "escalat", "Vorstand", "Flandorfer",
         "Preis", "price", "Termin", "timeline", "Entscheid", "decision", "blocker",
         "CEO", "budget", "Budget", "Angebot", "Ausschreibung", "next step", "Q3", "Q4"]


def main():
    dl = load_datalake()
    ms = datalake_get(dl, f"avoma_meetings?crm_opportunity_id=ilike.{OID}*&start_at=gte.2026-07-01&start_at=lt.2026-07-02&select=uuid,subject,start_at,duration&order=start_at.asc") or []
    print("yesterday's meetings:", [(m['subject'], m.get('duration')) for m in ms])
    for m in ms:
        tr = datalake_get(dl, f"avoma_transcripts?meeting_uuid=eq.{m['uuid']}&select=transcript_text&limit=1") or []
        txt = (tr[0].get("transcript_text") if tr else "") or ""
        print("\n" + "=" * 70)
        print(f"CALL: {m['subject']}  ({m.get('start_at','')[:16]}, {len(txt)} chars)")
        print("=" * 70)
        if not txt:
            print("(no transcript)")
            continue
        print("\n--- OPENING (first 1800 chars) ---")
        print(txt[:1800].replace("\n", " "))
        print("\n--- KEY MOMENTS (keyword windows) ---")
        seen = set()
        for t in TERMS:
            for mm in re.finditer(re.escape(t), txt, re.I):
                i = mm.start()
                key = i // 400
                if (t, key) in seen:
                    continue
                seen.add((t, key))
                print(f"\n[{t}] …{txt[max(0,i-160):i+200].strip().replace(chr(10),' ')}…")
                break  # first hit per term per call


if __name__ == "__main__":
    main()
