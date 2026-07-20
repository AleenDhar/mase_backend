import sys, time
seen=int(sys.argv[1]) if len(sys.argv)>1 else 0
LOG="cc_work/_fleet_gj.log"
def comps(txt):
    return [l for l in txt.splitlines() if l.strip().startswith("[") and (" OK " in l or " TO " in l)]
t0=time.time()
while time.time()-t0<105:
    try: txt=open(LOG,encoding="utf-8",errors="replace").read()
    except Exception: txt=""
    c=comps(txt)
    if len(c)>seen:
        for l in c[seen:]:
            print("LANDED:", l.strip()[:100])
        print(f"COUNT={len(c)}")
        if "FLEET-GJ-DONE" in txt: print("ALLDONE")
        sys.exit(0)
    if "FLEET-GJ-DONE" in txt:
        print(f"COUNT={len(c)}"); print("ALLDONE"); sys.exit(0)
    time.sleep(7)
c=comps(open(LOG,encoding="utf-8",errors="replace").read()) if True else []
last=(c[-1].strip()[:80] if c else "still scaling/sweeping — 0 landed")
print(f"NOCHANGE COUNT={len(c)} | {last}")
