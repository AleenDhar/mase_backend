import warnings; warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
cfg = {}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
SEL = ("account_name,opp_name,stage,updated_at,"
       "w:record->ai->deal_scores->headline->win_position,"
       "m:record->ai->deal_scores->headline->deal_momentum,"
       "dead:record->ai->deal_scores->headline->dead,"
       "eng:record->ai->scoring_studio->versions->win,"
       "la:record->hard->last_activity_date,thin:record->thin")
ids = ["006P700000QGAR3", "006P700000P69IM", "006P700000HBXgRIAX", "006P700000Xvjge"]
for oid in ids:
    # try full id, then 15-char prefix
    for q in (oid, oid[:15]):
        r = requests.get(f"{SB}/rest/v1/deal_records",
                         params={"select": SEL, "opp_id": f"like.{q}*"},
                         headers=H, verify=False, timeout=(10, 30)).json()
        if r:
            break
    x = r[0] if r else {}
    print(f"{oid}: {str(x.get('account_name'))[:28]:28} [{x.get('stage')}] "
          f"win={x.get('w')} mom={x.get('m')} dead={x.get('dead')} v{x.get('eng')} "
          f"last_act={x.get('la')} thin={x.get('thin')} updated={str(x.get('updated_at'))[:19]}")
