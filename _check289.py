import sys, json, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
cfg={}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local",encoding="utf-8"):
    l=l.strip()
    if l and not l.startswith("#") and "=" in l:
        k,v=l.split("=",1); cfg[k.strip()]=v.strip().strip('"').strip("'")
SB=cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K=cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH={"apikey":K,"Authorization":f"Bearer {K}"}
ids=[d["opp_id"] for d in json.load(open("cc_work/_combined_set.json",encoding="utf-8"))]
print(f"combined set = {len(ids)} opps")
SEL="opp_id,updated_at,eng:record->ai->scoring_studio->versions->win,w:record->ai->deal_scores->headline->win_position"
# fetch in chunks via in= filter
vers={}; nullw=0; fresh_today=0
for i in range(0,len(ids),80):
    chunk=ids[i:i+80]
    inlist="(" + ",".join(chunk) + ")"
    r=requests.get(f"{SB}/rest/v1/deal_records",params={"select":SEL,"opp_id":f"in.{inlist}"},headers=SH,verify=False,timeout=(10,90)).json()
    for x in r:
        e=str(x.get("eng"))
        vers[e]=vers.get(e,0)+1
        if x.get("w") is None: nullw+=1
        u=str(x.get("updated_at") or "")
        if u[:10]>="2026-07-13": fresh_today+=1
print("engine version distribution:", dict(sorted(vers.items())))
print(f"null win_position: {nullw} | updated on/after 2026-07-13: {fresh_today}")
