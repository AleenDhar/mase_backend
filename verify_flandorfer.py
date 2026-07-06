"""Wide-context check: what title (if any) do the calls attach to Flandorfer, and
who is called CFO / Vorstand / Finanz. Read-only."""
import re
from daily_summary.common import load_datalake, datalake_get

OID15 = "006P700000J71MD"


def main():
    dl = load_datalake()
    ms = datalake_get(dl, f"avoma_meetings?crm_opportunity_id=ilike.{OID15}*&select=uuid,subject&limit=60") or []
    corpus = ""
    for m in ms:
        rows = datalake_get(dl, f"avoma_transcripts?meeting_uuid=eq.{m['uuid']}&select=transcript_text&limit=1") or []
        if rows:
            corpus += "\n" + (rows[0].get("transcript_text") or "")
    print("corpus chars:", len(corpus))

    def wide(term, n=320, maxh=4):
        print(f"\n===== {term} =====")
        c = 0
        for mm in re.finditer(re.escape(term), corpus, re.I):
            i = mm.start()
            print("  …", corpus[max(0, i - n):i + len(term) + n].replace("\n", " ").strip(), "…")
            c += 1
            if c >= maxh:
                break
        if not c:
            print("  (none)")

    for t in ["Flandorfer", "CFO", "Finanzvorstand", "Vorstand", "Geschäftsführer",
              "Chief Financial", "Einkaufsleiter", "Head of Procurement"]:
        wide(t)


if __name__ == "__main__":
    main()
