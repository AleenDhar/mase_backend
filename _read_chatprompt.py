import warnings, json; warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
cfg = {}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
# The chat prompt lives in jarvis_settings, id='mase_chat_agent' (agent_prompt_store).
r = requests.get(f"{SB}/rest/v1/jarvis_settings", params={"id": "eq.mase_chat_agent", "select": "*"},
                 headers=H, verify=False, timeout=(10, 40))
print("HTTP", r.status_code)
rows = r.json() if r.status_code < 300 else r.text
if isinstance(rows, list) and rows:
    row = rows[0]
    print("columns:", list(row.keys()))
    sp = row.get("system_prompt")
    print(f"\n=== system_prompt ({len(str(sp)) if sp else 0} chars) | enabled_analysis_ids={row.get('enabled_analysis_ids')} ===\n")
    print(sp if sp else "(system_prompt is EMPTY/null -> the built-in _DEAL_ENGINE_CHAT_SYSTEM fallback is used)")
else:
    print("rows:", rows)
