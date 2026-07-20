"""Reproduce the AI-scorer 'no usable scores' locally: same prompt (_prompt with Studio
win/mom via requests shim), same evidence packet, direct Anthropic REST call (verify=False,
Zscaler-safe). Prints the raw model output head so we can SEE what shape it returns."""
import sys, os, json, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret()
for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY"):
    if sec.get(k):
        os.environ[k] = sec[k]
SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}

import scoring_studio as st


def active_locked_local():
    rows = requests.get(f"{SB}/rest/v1/scoring_instructions",
                        params={"locked": "eq.true", "select": "engine,version,content"},
                        headers=H, verify=VERIFY, timeout=60).json()
    def vk(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except ValueError:
            return (-1,)
    best = {}
    for r in rows:
        if r["version"] == "draft":
            continue
        e = r["engine"]
        if e not in best or vk(r["version"]) > vk(best[e]["version"]):
            best[e] = r
    return {e: ({"version": best[e]["version"], "content": best[e]["content"]} if e in best else None)
            for e in st.ASSETS}


st.active_locked = active_locked_local
import deal_engine_ai_scoring as A
import deal_engine_evidence as EV

sysp = A._prompt()
print("scorer system prompt:", len(sysp), "chars | governed:",
      "GOVERNING ENGINE — Zycus Win Position" in sysp, "| mom version:",
      ("v10.5" if "Deal Momentum · LOCKED v10.5" in sysp else "?"))

# real record (John Deere) from the dry-run output
rec = json.load(open("dryrun_forecasted/006P700000KHd9V.json", encoding="utf-8"))
packet = EV.build_evidence_packet(rec)
user = ("Score this opportunity. Evidence packet (facts only):\n\n"
        + json.dumps(packet, default=str, ensure_ascii=False))
print("packet chars:", len(user))

r = requests.post("https://api.anthropic.com/v1/messages",
                  headers={"x-api-key": sec["ANTHROPIC_API_KEY"],
                           "anthropic-version": "2023-06-01",
                           "content-type": "application/json"},
                  json={"model": "claude-sonnet-5", "max_tokens": 4000,
                        "system": sysp,
                        "messages": [{"role": "user", "content": user}]},
                  verify=False, timeout=300)
print("HTTP", r.status_code)
if r.status_code >= 300:
    print(r.text[:600])
    sys.exit(1)
out = r.json()
text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
print("stop_reason:", out.get("stop_reason"), "| output chars:", len(text))
print("\n--- RAW HEAD (600) ---")
print(text[:600])
print("\n--- RAW TAIL (300) ---")
print(text[-300:])
from opportunity_analyzer import _extract_json
parsed = _extract_json(text)
ok = isinstance(parsed, dict) and parsed.get("scores")
print("\n_extract_json usable scores?:", bool(ok))
if ok:
    print("scores:", json.dumps(parsed["scores"]))
