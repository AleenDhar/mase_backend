"""Find Austrian Post's real CFO + Flandorfer's real title via Apollo (fallback
Lusha). Read-only enrichment lookup. Creds from AWS Secrets Manager."""
import json, requests, urllib3
from daily_summary.common import load_secret
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DOMAINS = ["post.at"]


def apollo_search(key, body):
    h = {"Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": key}
    r = requests.post("https://api.apollo.io/v1/mixed_people/search", json=body, headers=h,
                      verify=False, timeout=45)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)


def show(label, code, data):
    print(f"\n=== {label} (HTTP {code}) ===")
    if not isinstance(data, dict):
        print("  ", str(data)[:300]); return
    ppl = data.get("people") or data.get("contacts") or []
    pg = (data.get("pagination") or {})
    print(f"  results: {len(ppl)} (total {pg.get('total_entries','?')})")
    for p in ppl[:15]:
        org = (p.get("organization") or {}).get("name") or p.get("organization_name") or ""
        print(f"   - {p.get('name','?'):28} | {(p.get('title') or '')[:48]:48} | {org}")


def main():
    sec = load_secret()
    key = sec.get("APOLLO_API_KEY")

    # 1) finance leaders at Austrian Post
    code, data = apollo_search(key, {
        "q_organization_domains": "\n".join(DOMAINS),
        "person_titles": ["Chief Financial Officer", "CFO", "Finance Director",
                          "Head of Finance", "Finanzvorstand", "VP Finance"],
        "person_seniorities": ["c_suite", "vp", "director", "head"],
        "page": 1, "per_page": 25,
    })
    show("Austrian Post — finance leaders", code, data)

    # 2) the whole board / c-suite
    code, data = apollo_search(key, {
        "q_organization_domains": "\n".join(DOMAINS),
        "person_seniorities": ["c_suite", "owner", "founder"],
        "page": 1, "per_page": 25,
    })
    show("Austrian Post — C-suite / board", code, data)

    # 3) Flandorfer specifically
    code, data = apollo_search(key, {
        "q_organization_domains": "\n".join(DOMAINS),
        "q_keywords": "Flandorfer",
        "page": 1, "per_page": 10,
    })
    show("Austrian Post — 'Flandorfer'", code, data)

    # 4) Flandorfer global (in case wrong domain)
    code, data = apollo_search(key, {"q_keywords": "Flandorfer Post", "page": 1, "per_page": 10})
    show("'Flandorfer Post' — global", code, data)


if __name__ == "__main__":
    main()
