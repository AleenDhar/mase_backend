"""Completeness check: does Bandhan's deal_records row have EVERY drawer section populated?"""
import sys, warnings
warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
cfg={}
for l in open(r'C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local',encoding='utf-8'):
    l=l.strip()
    if l and not l.startswith('#') and '=' in l:
        k,v=l.split('=',1); cfg[k.strip()]=v.strip().strip('"').strip("'")
SB=cfg['NEXT_PUBLIC_SUPABASE_URL'].rstrip('/'); K=cfg['SUPABASE_SERVICE_ROLE_KEY']; SH={'apikey':K,'Authorization':'Bearer '+K}
OID='006P700000H55TV'
r=requests.get(f'{SB}/rest/v1/deal_records',params={'select':'updated_at,swept_at,record','opp_id':f'eq.{OID}'},headers=SH,verify=False,timeout=90).json()
if not r:
    print("NO ROW"); sys.exit()
rec=r[0]['record'] or {}; ai=rec.get('ai') or {}; cov=rec.get('evidence_coverage') or {}
ds=ai.get('deal_scores') or {}; hl=ds.get('headline') or {}; sv=(ai.get('scoring_studio') or {}).get('versions') or {}
print(f"BANDHAN BANK  updated={r[0]['updated_at'][:19]}  swept={r[0].get('swept_at')}")
print(f"  engine win=v{sv.get('win')} mom=v{sv.get('mom')}  factor_source={ds.get('factor_source')}  calls_read={cov.get('calls_read')}\n")

def has(v):
    if v is None: return False
    if isinstance(v,(list,dict,str)): return len(v)>0
    return True

def n(v):
    if isinstance(v,dict) and 'items' in v: return len(v['items'])
    if isinstance(v,list): return len(v)
    if isinstance(v,dict): return len(v)
    if isinstance(v,str): return len(v)
    return v

SECTIONS=[
    ("DEAL SCORES — win", hl.get('win_position')),
    ("DEAL SCORES — momentum", hl.get('deal_momentum')),
    ("DEAL SCORES — commitment", hl.get('customer_commitment')),
    ("DEAL SCORES — risk", hl.get('deal_risk')),
    ("DEAL SCORES — read label", hl.get('read')),
    ("SCORE REASONS — win", (ds.get('ai_reasons') or {}).get('win_position')),
    ("SCORE REASONS — momentum", (ds.get('ai_reasons') or {}).get('deal_momentum')),
    ("SCORE REASONS — commitment", (ds.get('ai_reasons') or {}).get('customer_commitment')),
    ("SCORE REASONS — risk", (ds.get('ai_reasons') or {}).get('deal_risk')),
    ("24H SUMMARY (day_summary)", ai.get('day_summary')),
    ("TO-DOS (recommended_moves)", ai.get('recommended_moves')),
    ("STAKEHOLDER MAP", ai.get('stakeholder_map')),
    ("MEDDPICC", ai.get('meddpicc')),
    ("CRITICAL SIGNALS", ai.get('critical_signals')),
    ("COMPETITIVE POSITION", ai.get('competitive_position')),
    ("FORECAST READ", ai.get('forecast_read')),
    ("NORTH STAR VERDICT", ai.get('north_star_verdict')),
    ("CEO INTERVENTION", ai.get('ceo_intervention')),
    ("BUSINESS CASE", ai.get('business_case')),
    ("PRODUCT SCOPE", ai.get('product_scope')),
]
miss=[]
for name,v in SECTIONS:
    ok=has(v); 
    if not ok: miss.append(name)
    print(f"  [{'OK ' if ok else 'MISSING'}] {name:32} {('('+str(n(v))+')') if ok else ''}")
print()
print("ALL SECTIONS PRESENT" if not miss else f"MISSING {len(miss)}: "+", ".join(miss))
