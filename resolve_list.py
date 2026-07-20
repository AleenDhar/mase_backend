"""Resolve the APAC/EMEA deal-name list -> opp_ids (read-only). People excluded upstream."""
import sys, warnings, json
warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
cfg={}
for l in open(r'C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local',encoding='utf-8'):
    l=l.strip()
    if l and not l.startswith('#') and '=' in l:
        k,v=l.split('=',1); cfg[k.strip()]=v.strip().strip('"').strip("'")
SB=cfg['NEXT_PUBLIC_SUPABASE_URL'].rstrip('/'); K=cfg['SUPABASE_SERVICE_ROLE_KEY']; SH={'apikey':K,'Authorization':'Bearer '+K}

# name -> search term(s). People (Tanmay, Luke, Hakim) intentionally excluded.
NAMES = ["Gamuda","FGV","port","Bank Rakyat","EPF","Cebu","International SOS","Nidec",
         "Jockey Club","Temasek","WIK","Haeco","MTR","SATS","Changi","Bandhan","Angel one",
         "Vodafone","Thiess","Civeo","PNG","Port Authority NSW","Techtronic","Scheme",
         "WA DOJ","Ausnet","Domino","Orascom","Fly Dubai","ASYAD","Arabian Industries",
         "Turn Well","Khansaheb","Wheelson","Mumtalakat","Wealth fund","SAMI","Alghanim","DWTC"]

def search(term):
    hits=[]
    for col in ("account_name","opp_name"):
        try:
            r=requests.get(f"{SB}/rest/v1/deal_records",
                params={"select":"opp_id,account_name,opp_name,stage,forecast_category","active":"eq.true",
                        col:f"ilike.*{term}*"},headers=SH,verify=False,timeout=60).json()
            if isinstance(r,list): hits+=r
        except Exception as e: pass
    seen={}; 
    for h in hits:
        seen[h['opp_id']]=h
    return list(seen.values())

resolved={}; unresolved=[]; ambiguous={}
for name in NAMES:
    h=search(name)
    if not h:
        unresolved.append(name); print(f"  [MISS ] {name}")
    elif len(h)==1:
        x=h[0]; resolved[x['opp_id']]=x
        print(f"  [OK   ] {name:22} -> {x['opp_id']}  {str(x['account_name'])[:30]:32} {x['stage']}")
    else:
        ambiguous[name]=h
        print(f"  [MULTI] {name:22} -> {len(h)} matches:")
        for x in h[:5]:
            resolved.setdefault(x['opp_id'],x)
            print(f"           {x['opp_id']}  {str(x['account_name'])[:30]:32} {str(x['opp_name'])[:24]} {x['stage']}")

print(f"\n=== {len(resolved)} unique opps resolved; {len(unresolved)} unresolved; {len(ambiguous)} ambiguous ===")
print("UNRESOLVED:", ", ".join(unresolved))
json.dump({"resolved":{k:v['account_name'] for k,v in resolved.items()},"unresolved":unresolved,
           "ambiguous":{k:[x['opp_id'] for x in v] for k,v in ambiguous.items()}},
          open("cc_work/_list_resolved.json","w"),indent=1)
print("wrote cc_work/_list_resolved.json")
