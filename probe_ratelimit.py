"""Read the org's LIVE Anthropic rate-limit headers (-> tier) and prove prompt
caching returns hits, using the real ANTHROPIC_API_KEY. Never prints the key."""
import json, warnings, sys
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# key from the cached app env (same source boot_env uses); never echoed
env = json.load(open(".mase_app_env.json"))
KEY = env.get("ANTHROPIC_API_KEY") or ""
if not KEY:
    print("no ANTHROPIC_API_KEY in .mase_app_env.json"); sys.exit(1)
H = {"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-5"  # what the sweep runs (_FRONTIER_DEFAULT)

# A ≥2048-token shared system prefix so Sonnet 5 will actually cache it.
BIG = ("You are a revenue-intelligence analyst. " * 400)  # ~2.4k tokens

def call(tag):
    body = {"model": MODEL, "max_tokens": 8,
            "system": [{"type": "text", "text": BIG, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
    r = requests.post(URL, headers=H, json=body, verify=False, timeout=(10, 60))
    hd = r.headers
    u = (r.json() or {}).get("usage", {}) if r.status_code < 300 else {}
    return r.status_code, hd, u

print("Probing live rate-limit headers + cache behaviour (model=%s) …\n" % MODEL, flush=True)
sc1, hd, u1 = call("call1")
if sc1 >= 300:
    print("HTTP", sc1, "-", requests.post(URL, headers=H, json={"model":MODEL,"max_tokens":8,"messages":[{"role":"user","content":"hi"}]}, verify=False, timeout=(10,60)).text[:300])
    sys.exit(1)
sc2, _, u2 = call("call2")  # identical prefix -> should read cache

def g(k):
    return hd.get(k, "?")

rpm = g("anthropic-ratelimit-requests-limit")
itpm = g("anthropic-ratelimit-input-tokens-limit")
otpm = g("anthropic-ratelimit-output-tokens-limit")
print("=== LIVE RATE LIMITS (org level, %s bucket) ===" % MODEL)
print(f"  RPM  (requests/min) : {rpm}")
print(f"  ITPM (input tok/min): {itpm}")
print(f"  OTPM (output tok/min): {otpm}")
print(f"  remaining now: req={g('anthropic-ratelimit-requests-remaining')} "
      f"in_tok={g('anthropic-ratelimit-input-tokens-remaining')} "
      f"out_tok={g('anthropic-ratelimit-output-tokens-remaining')}")

# tier inference from the published Sonnet-5 table
def tier():
    try:
        i = int(itpm)
    except Exception:
        return "unknown"
    return {2_000_000: "START ($500/mo cap)",
            5_000_000: "BUILD ($1,000/mo cap)",
            10_000_000: "SCALE ($200,000/mo cap)"}.get(i, "CUSTOM/other (i=%d)" % i)
print(f"  => Usage tier: {tier()}")

print("\n=== PROMPT CACHING (2 identical calls) ===")
print(f"  call1: cache_write={u1.get('cache_creation_input_tokens')} "
      f"cache_read={u1.get('cache_read_input_tokens')} input={u1.get('input_tokens')}")
print(f"  call2: cache_write={u2.get('cache_creation_input_tokens')} "
      f"cache_read={u2.get('cache_read_input_tokens')} input={u2.get('input_tokens')}")
cr = u2.get("cache_read_input_tokens") or 0
print("  => caching WORKS (call2 read %d tokens from cache, billed ~10%%, free vs ITPM)" % cr
      if cr > 0 else "  => cache MISS on call2 — investigate (prefix too short or invalidated)")
print("PROBE-DONE")
