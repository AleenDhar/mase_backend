import warnings, json, sys
warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
cfg={}
for l in open(r'C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local',encoding='utf-8'):
    l=l.strip()
    if l and not l.startswith('#') and '=' in l:
        k,v=l.split('=',1); cfg[k.strip()]=v.strip().strip('"').strip("'")
SB=cfg['NEXT_PUBLIC_SUPABASE_URL'].rstrip('/'); K=cfg['SUPABASE_SERVICE_ROLE_KEY']; SH={'apikey':K,'Authorization':'Bearer '+K}
NAMES=["Etex","Moore","Nutreco","Ferrero","Vestacy","Vestas","PV Group","Hager","EVN","Evonik",
       "Erste","Deutsche Telekom","Kromberg","Lapp","Austrian Post","Bosch","Ahlstrom","ASSA Abloy"]
def search(term):
    hits=[]
    for col in ("account_name","opp_name"):
        try:
            r=requests.get(SB+'/rest/v1/deal_records',params={'select':'opp_id,account_name,opp_name,stage,forecast_category','active':'eq.true',col:f'ilike.*{term}*'},headers=SH,verify=False,timeout=60).json()
            if isinstance(r,list): hits+=r
        except Exception: pass
    seen={};
    for h in hits: seen[h['opp_id']]=h
    return list(seen.values())
res={}; miss=[]
for n in NAMES:
    h=search(n)
    if not h: miss.append(n); print(f"  [MISS ] {n}")
    else:
        for x in h[:3]:
            res.setdefault(x['opp_id'],x)
        tag='' if len(h)==1 else f' ({len(h)} matches)'
        for x in h[:3]:
            print(f"  [{'OK' if len(h)==1 else 'MUL'}] {n:18} -> {x['opp_id']}  {str(x['account_name'])[:30]:32} {x['stage']}{tag}")
print(f"\n{len(res)} unique opps; MISS: {', '.join(miss)}")
json.dump({o:v['account_name'] for o,v in res.items()}, open('cc_work/_emea.json','w'))
