#!/usr/bin/env python3
"""
Per-account logo enrichment via Apollo — RUN ON THE BACKEND (AWS), not a laptop.

Why backend-only:
  - Apollo's `logo_url` is dropped by the DeepAgent MCP summarizer, so we call Apollo's
    REST API directly with APOLLO_API_KEY (a backend secret) — raw response, real logo_url.
  - The office Zscaler proxy blocks Supabase Storage *uploads* from laptops; the backend
    is not behind it, so uploads succeed here.

What it does (idempotent, safe to re-run):
  1. Distinct accounts from Supabase `opportunity_cache`, scoped to the TRACKED book
     (in-scope owners per lib/engine/helpers.ts OWNER_VP, open deals only) — ~388 accounts,
     NOT the full ~935-row cache.
  2. Skip accounts that already have a logo in the `account-logos` bucket.
  3. Apollo: search by name -> best org -> logo_url (+ primary_domain); if the search row
     has no logo_url, enrich by the resolved domain. Reject mismatched names.
  4. Download the logo and upload to Supabase Storage `account-logos/<slug>.png`
     (the bucket the frontend already reads via signed URLs).
  5. Write a private `manifest.json` (slug -> {account, domain, logo_url, source}).

After it runs on the backend, refresh the frontend display map from a machine that can
READ Supabase (signing/reads are NOT Zscaler-blocked):
    cd frontend && python gen_logo_map.py     # regenerates lib/engine/accountLogos.ts

Env (already present in the backend / Secrets Manager):
    APOLLO_API_KEY                              (required)
    APOLLO_BASE_URL                             (optional, default https://api.apollo.io/api/v1)
    SUPABASE_URL or NEXT_PUBLIC_SUPABASE_URL    (required)
    SUPABASE_SERVICE_ROLE_KEY                   (required)

Run:
    python enrich_account_logos.py                 # all tracked accounts missing a logo
    python enrich_account_logos.py --limit 25      # first 25 (smoke test)
    python enrich_account_logos.py --dry-run       # resolve + report, upload nothing
"""
import os, re, sys, ssl, json, time, urllib.request, urllib.error, urllib.parse, concurrent.futures

# ---- config -----------------------------------------------------------------
APOLLO_KEY = os.environ.get("APOLLO_API_KEY")
APOLLO_BASE = os.environ.get("APOLLO_BASE_URL", "https://api.apollo.io/api/v1").rstrip("/")
SB = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
if not (APOLLO_KEY and SB and SB_KEY):
    sys.exit("Set APOLLO_API_KEY, SUPABASE_URL (or NEXT_PUBLIC_SUPABASE_URL), SUPABASE_SERVICE_ROLE_KEY")

DRY = "--dry-run" in sys.argv
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
WORKERS = 4  # keep modest — Apollo rate-limits

CTX = ssl.create_default_context()  # backend has clean egress; verify TLS normally
SBH = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
APOLLO_H = {"Content-Type": "application/json", "Cache-Control": "no-cache",
            "Accept": "application/json", "X-Api-Key": APOLLO_KEY}

# Tracked book = in-scope owners. KEEP IN SYNC with lib/engine/helpers.ts OWNER_VP.
TRACKED_OWNERS = {
    "Anthony Gray", "Claire Hudson", "Casper Hoeholt", "John Woodcock", "Caroline Lacocque",
    "Dirk Fischbach", "Pierre Meraud", "Monika Mutscher", "Carl Kimball", "Mohamad Alhakim",
    "Dan Quinn", "Adam Hasan", "George John", "Guillaume Pasquet", "Luke Dougherty",
    "Tanmay Srivastava", "Alexa Bradley", "Karson Keogh", "Mario Castro", "Rick Taranek",
    "Kevin Cipollaro", "Edward Dlugosz", "Marc Quessenberry", "Richard Hunsinger",
    "Mike Flowers", "Arthur Raguette", "Michael McCarthy", "Bailey Erazo", "Grace Kim",
    "Justin Ajmo", "Steve Ovadje",
}


# ---- http helpers -----------------------------------------------------------
def http(url, method="GET", data=None, headers=None, timeout=30):
    r = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    return urllib.request.urlopen(r, context=CTX, timeout=timeout)


def http_json(url, method="GET", body=None, headers=None, timeout=30, retry=2):
    try:
        data = json.dumps(body).encode() if body is not None else None
        resp = http(url, method, data, headers, timeout)
        return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (429, 500, 502, 503) and retry > 0:
            time.sleep(2.0)
            return http_json(url, method, body, headers, timeout, retry - 1)
        raise
    except Exception:
        if retry > 0:
            time.sleep(1.2)
            return http_json(url, method, body, headers, timeout, retry - 1)
        raise


def slug(s):
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", s.lower()))[:60]


# ---- name matching (reject wrong Apollo hits) -------------------------------
LEGAL = re.compile(r"\b(inc|incorporated|ltd|limited|llc|llp|corp|corporation|co|company|"
                   r"group|groupe|holdings?|plc|sa|ag|nv|bv|gmbh|pvt|private|pte|sas|srl|spa|"
                   r"technologies|technology|solutions|systems|software|services|international|"
                   r"global|the)\b\.?", re.I)


def clean_name(nm):
    x = LEGAL.sub(" ", re.sub(r"[,.()/]", " ", nm))
    return re.sub(r"\s+", " ", x).strip() or nm


def norm(s):
    return re.sub(r"[^a-z0-9]", "", clean_name(s).lower())


def good_match(acct, cand_name, domain):
    a, s = norm(acct), norm(cand_name or "")
    d = re.sub(r"[^a-z0-9]", "", (domain or "").split(".")[0])
    if not a:
        return False
    if a == s or a == d:
        return True
    if len(a) >= 4 and (s.startswith(a) or a.startswith(s) or d.startswith(a) or a.startswith(d)):
        return True
    toks = [t for t in clean_name(acct).lower().split() if len(t) >= 4]
    return bool(toks and (toks[0] in s or toks[0] in d))


# ---- apollo -----------------------------------------------------------------
def apollo_search(name):
    """Search Apollo companies by name; return the best org dict (raw, has logo_url)."""
    try:
        d = http_json(f"{APOLLO_BASE}/mixed_companies/search", "POST",
                      {"q_organization_name": name, "page": 1, "per_page": 5}, APOLLO_H)
        return d.get("organizations") or d.get("accounts") or []
    except Exception:
        return []


def apollo_enrich(domain):
    """Enrich one org by domain; return the org dict (raw, has logo_url)."""
    try:
        d = http_json(f"{APOLLO_BASE}/organizations/enrich?domain={urllib.parse.quote(domain)}",
                      "POST", {}, APOLLO_H)
        return d.get("organization") or {}
    except Exception:
        return {}


def resolve_logo(account_name):
    """Return (logo_url, domain, matched_name) or (None, None, None)."""
    orgs = apollo_search(account_name)
    best = None
    for o in orgs:
        dom = o.get("primary_domain") or o.get("domain") or ""
        if good_match(account_name, o.get("name"), dom):
            best = o
            break
    if not best and orgs:
        # fall back to the top hit only if its name clearly relates
        o = orgs[0]
        if good_match(account_name, o.get("name"), o.get("primary_domain") or ""):
            best = o
    if not best:
        return None, None, None
    dom = best.get("primary_domain") or best.get("domain") or ""
    logo = best.get("logo_url")
    if not logo and dom:
        logo = apollo_enrich(dom).get("logo_url")
    return (logo or None), (dom or None), best.get("name")


# ---- supabase storage -------------------------------------------------------
def bucket_existing():
    have, off = set(), 0
    while True:
        objs = http_json(f"{SB}/storage/v1/object/list/account-logos", "POST",
                         {"prefix": "", "limit": 1000, "offset": off},
                         {**SBH, "Content-Type": "application/json"})
        if not objs:
            break
        for o in objs:
            n = o.get("name", "")
            if n.endswith(".png"):
                have.add(n[:-4])
        off += len(objs)
        if len(objs) < 1000:
            break
    return have


def upload_png(slug_, content, ctype):
    http(f"{SB}/storage/v1/object/account-logos/{slug_}.png", "POST", content,
         {**SBH, "Content-Type": ctype or "image/png", "x-upsert": "true"})


def download(url):
    try:
        r = http(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status == 200:
            b = r.read()
            ct = r.headers.get("content-type", "")
            if b and len(b) >= 200 and ("image" in ct or url.lower().endswith((".png", ".jpg", ".jpeg", ".ico"))):
                return b, (ct or "image/png")
    except Exception:
        pass
    return None, None


# ---- gather tracked accounts ------------------------------------------------
def tracked_accounts():
    accounts, off = {}, 0
    while True:
        u = (f"{SB}/rest/v1/opportunity_cache?select=account_id,account_name,owner_name,is_closed"
             f"&order=account_name&limit=1000&offset={off}")
        rows = http_json(u, headers=SBH)
        if not rows:
            break
        for r in rows:
            if r.get("owner_name") not in TRACKED_OWNERS or r.get("is_closed"):
                continue
            nm = (r.get("account_name") or "").strip()
            sg = slug(nm)
            if nm and sg and sg not in accounts:
                accounts[sg] = {"slug": sg, "account_name": nm, "account_id": r.get("account_id")}
        off += len(rows)
        if len(rows) < 1000:
            break
    return accounts


# ---- main -------------------------------------------------------------------
def main():
    accounts = tracked_accounts()
    have = bucket_existing()
    todo = [a for a in accounts.values() if a["slug"] not in have]
    if LIMIT:
        todo = todo[:LIMIT]
    print(f"tracked={len(accounts)}  already_have_logo={len(have & set(accounts))}  to_enrich={len(todo)}"
          f"{'  [DRY-RUN]' if DRY else ''}", flush=True)

    results = []

    def work(a):
        logo_url, dom, matched = resolve_logo(a["account_name"])
        if not logo_url:
            return {**a, "domain": dom, "logo_url": None, "stored": False}
        if DRY:
            return {**a, "domain": dom, "logo_url": logo_url, "matched": matched, "stored": False}
        body, ct = download(logo_url)
        if not body:
            return {**a, "domain": dom, "logo_url": logo_url, "stored": False}
        try:
            upload_png(a["slug"], body, ct)
            stored = True
        except Exception:
            stored = False
        return {**a, "domain": dom, "logo_url": logo_url, "matched": matched, "stored": stored}

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, res in enumerate(ex.map(work, todo)):
            results.append(res)
            if (i + 1) % 25 == 0:
                n = sum(1 for r in results if r.get("logo_url"))
                print(f"  {i+1}/{len(todo)}  ({n} logos resolved)", flush=True)

    resolved = [r for r in results if r.get("logo_url")]
    stored = [r for r in results if r.get("stored")]
    print(f"RESOLVED {len(resolved)}/{len(todo)} logos; STORED {len(stored)}", flush=True)
    for r in resolved[:12]:
        print(f"   {'OK' if r['stored'] else '~~'} {r['account_name'][:34]:34} {r.get('domain') or ''}", flush=True)

    if not DRY:
        manifest = {r["slug"]: {"account_name": r["account_name"], "account_id": r.get("account_id"),
                                "domain": r.get("domain"), "logo_url": r.get("logo_url"),
                                "source": "apollo" if r.get("stored") else None} for r in results}
        try:
            http(f"{SB}/storage/v1/object/account-logos/manifest_apollo.json", "POST",
                 json.dumps({"resolved": len(resolved), "stored": len(stored), "total": len(todo),
                             "logos": manifest}).encode(),
                 {**SBH, "Content-Type": "application/json", "x-upsert": "true"})
            print("manifest_apollo.json written (private)", flush=True)
        except Exception as e:
            print("manifest write failed:", e, flush=True)
    print("Done. Next: `cd frontend && python gen_logo_map.py` to refresh the display map.", flush=True)


if __name__ == "__main__":
    main()
