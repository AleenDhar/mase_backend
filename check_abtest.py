"""Read-only: poll datalake.ab_test_results for the named dry-run opps."""
import dryrun_fleet as D
for label, oid in [("Allstate", "006P7000006uKrq"), ("Telcel", "006P700000aBK6l"),
                   ("GSK", "006P700000aZ93k"), ("Saudia", "006P700000aEeX8")]:
    st = D.poll(oid) or {}
    has_rec = bool((st.get("result") or {}).get("record")) if isinstance(st.get("result"), dict) else False
    print(f"{label:10} {oid} -> status={st.get('status')} has_record={has_rec} err={str(st.get('error'))[:70]}")
