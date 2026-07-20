import sys, csv, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
cfg={}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local",encoding="utf-8"):
    l=l.strip()
    if l and not l.startswith("#") and "=" in l:
        k,v=l.split("=",1); cfg[k.strip()]=v.strip()
SB=cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K=cfg["SUPABASE_SERVICE_ROLE_KEY"]; SH={"apikey":K,"Authorization":"Bearer "+K}
# label, oid, screenshot win/mom (v10.7 baseline the user saw)
D=[("Temasek","006P700000BV2eA",51,96),("Techtronic","006P700000GWfrf",52,99),
   ("SAMI","006P700000RD9Ir",39,54),("Wheelson","006P700000VlPdp",52,52),
   ("Mair Group","006P700000PtQGP",99,95),("MTR","006P700000KTTO5",60,79),
   ("Khansaheb","006P700000LtIUv",55,48),("HAECO","006P700000NwbBd",36,59),
   ("Globe Telecom","006P7000008hZHF",49,57),("Gamuda","006P700000Q15OU",52,56),
   ("Cebu Pacific","0066700000wdNe1",52,68),("Bandhan Bank","006P700000H55TV",70,74),
   ("Arabian Ind","006P700000QvP7Z",70,58),("ACEN","006P700000DkWgX",20,8),
   ("Robert Bosch","006P700000PlMpu",36,31)]
SEL="stage,updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,cov:record->evidence_coverage"
print("="*104)
print("%-14s %-19s %9s %9s %6s %6s %-15s"%("deal","stage","WIN(was)","MOM(was)","src","calls","read"))
print("="*104)
rows=[]; clean=0; blind=[]
for lbl,o,pw,pm in D:
    d=requests.get(SB+"/rest/v1/deal_records",params={"select":SEL,"opp_id":"eq."+o},headers=SH,verify=False,timeout=60).json()[0]
    ds=d.get("scores") or {}; hl=ds.get("headline") or {}; sv=(d.get("studio") or {}).get("versions") or {}; cov=d.get("cov") or {}
    w=hl.get("win_position"); m=hl.get("deal_momentum"); src=ds.get("factor_source"); cr=cov.get("calls_read")
    v108 = str(sv.get("win"))=="10.8" and src=="ai"; clean += v108
    if cr in (0,None): blind.append(lbl)
    dw="%s(%s)"%(w,pw); dm="%s(%s)"%(m,pm)
    print("%-14s %-19s %9s %9s %6s %6s %-15s"%(lbl,str(d.get("stage"))[:19],dw,dm,src,cr,str(hl.get("read"))[:15]))
    rows.append([lbl,o,d.get("stage"),w,m,hl.get("customer_commitment"),hl.get("deal_risk"),hl.get("read"),
                 src,sv.get("win"),cr,pw,pm,
                 " || ".join("[%s] %s"%(b.get("tone"),b.get("text")) for b in (ds.get("ai_reasons") or {}).get("win_position") or []),
                 " || ".join("[%s] %s"%(b.get("tone"),b.get("text")) for b in (ds.get("ai_reasons") or {}).get("deal_momentum") or [])])
print("\n%d/15 clean v10.8 (src=ai). scored on 0 calls (CRM-only, needs a badge): %s"%(clean, ", ".join(blind) or "none"))
with open("fleet_v108_FINAL.csv","w",newline="",encoding="utf-8-sig") as fh:
    w=csv.writer(fh); w.writerow(["deal","opp_id","stage","win","momentum","commitment","risk","read","factor_source","win_engine","calls_read","prev_win_v107","prev_mom_v107","win_reasons","momentum_reasons"])
    w.writerows(rows)
print("wrote fleet_v108_FINAL.csv")
