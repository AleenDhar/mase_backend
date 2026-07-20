import os
import dryrun_fleet as D
print("=== CLOUD sweep (td281) — Allstate + Cebu ===")
for lbl, o in [("Allstate", "006P7000006uKrq"), ("Cebu Pacific Air", "0066700000wdNe1")]:
    st = D.poll(o) or {}
    res = st.get("result") if isinstance(st.get("result"), dict) else {}
    rec = (res or {}).get("record")
    hl = (((rec or {}).get("ai") or {}).get("deal_scores") or {}).get("headline") or {}
    print(f"  {lbl:18} status={st.get('status')} win={hl.get('win_position')} "
          f"mom={hl.get('deal_momentum')} err={str(st.get('error'))[:50]}")
p = "cc_work/006P700000DkWgX.json"
print("\n=== LOCAL ACEN synthesis ===")
print("  cc_work/006P700000DkWgX.json:",
      (f"{os.path.getsize(p)} bytes @ ok" if os.path.exists(p) else "not written yet (subagent still running)"))
