import py_compile
py_compile.compile("deal_engine_sweep.py", doraise=True)
print("deal_engine_sweep.py compiles OK")
# Replicate the exact gate (keep_lm defaults true).
keep_lm = True
def frm(source):
    return (source in ("update_living_memory", "salesforce_trigger", "salesforce")) or not keep_lm
print("=== from_scratch by source (keep_lm=true) ===")
for s in ["update_living_memory", "salesforce_trigger", "salesforce", "manual", "worker", "", None]:
    print(f"  source={str(s):22} -> from_scratch={frm(s)}")
print("EXPECT: update_living_memory/salesforce_trigger/salesforce=True (from-scratch);")
print("        manual/worker/blank=False (KEEP living memory) — trigger scoped correctly.")
