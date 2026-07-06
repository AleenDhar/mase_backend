"""Fetch Austrian Post Avoma transcripts from the datalake and check the CEO-help
claims against what was actually said. Read-only."""
import re, json
from daily_summary.common import load_datalake, datalake_get

OID15 = "006P700000J71MD"
TERMS = ["Flandorfer", "Flander", "Tronkeon", "Trakeon", "170", "man-day", "manday",
         "S/4HANA", "S4HANA", "HANA", "CFO", "economic buyer", "front-runner",
         "front runner", "POC", "Pölki", "Polki", "orchestration", "overlay"]


def ctx(text, term, n=180):
    out = []
    for m in re.finditer(re.escape(term), text, re.I):
        i = m.start()
        out.append(text[max(0, i - n):i + len(term) + n].replace("\n", " "))
        if len(out) >= 2:
            break
    return out


def main():
    dl = load_datalake()

    # 1) schema probe
    probe = datalake_get(dl, "avoma_transcripts?select=*&limit=1") or []
    tkeys = list(probe[0].keys()) if probe else []
    print("avoma_transcripts columns:", tkeys)
    # pick the meeting-link key + the text key
    link = next((k for k in ("uuid", "meeting_uuid", "avoma_uuid", "meeting_id", "call_uuid") if k in tkeys), "uuid")
    textcol = next((k for k in ("transcript_text", "text", "transcript", "content") if k in tkeys), "transcript_text")
    print("using link key:", link, "| text col:", textcol)

    # 2) opp meetings
    ms = datalake_get(dl, f"avoma_meetings?crm_opportunity_id=ilike.{OID15}*&select=uuid,subject,start_at,transcript_ready&order=start_at.desc&limit=60") or []
    print(f"\nAustrian Post meetings in datalake: {len(ms)}")

    corpus = ""
    per = []
    for m in ms:
        uu = m.get("uuid")
        rows = datalake_get(dl, f"avoma_transcripts?{link}=eq.{uu}&select={textcol}&limit=1") or []
        txt = (rows[0].get(textcol) if rows else "") or ""
        per.append((m.get("start_at", "")[:10], m.get("subject"), len(txt)))
        corpus += "\n" + txt
    print("transcripts pulled; total chars:", len(corpus))
    print("\nper-meeting (date | chars | subject):")
    for d, s, n in per[:30]:
        print(f"   {d} | {n:7d} | {(s or '')[:60]}")

    print("\n===== CLAIM CHECK (term : #hits in transcripts) =====")
    for t in TERMS:
        hits = len(re.findall(re.escape(t), corpus, re.I))
        print(f"   {t:16} : {hits}")

    print("\n===== CONTEXT for the key claims =====")
    for t in ["Flandorfer", "Flander", "Tronkeon", "170", "HANA", "CFO"]:
        snips = ctx(corpus, t)
        if snips:
            print(f"\n--- {t} ---")
            for s in snips:
                print("   …", s.strip(), "…")


if __name__ == "__main__":
    main()
