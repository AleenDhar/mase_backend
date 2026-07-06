"""Refresh the human-readable "Scores & Reasons" PANEL for ALL active deals so the
2026-07 tweaks land everywhere — WITHOUT recomputing the score numbers.

build_cro_panel(record) reads the EXISTING ai.deal_scores.headline + the sweep's
narratives, and now produces: narrative per-factor bullets (economic_buyer /
competition / paper_process narratives, competitive & champion summaries,
customer_preference) instead of bare labels, the top RISKS folded into the win
block, a "Why this number" label, and a plain-english "do nothing" read.

We deliberately do NOT re-run compute_deal_scores here: the score is meant to be
computed at SWEEP TIME over FRESH footprints/signals. Recomputing it offline over a
stale record diverges wildly (dark/stalled deals drag to ~0) — that's a sweep's job,
not a panel refresh. The forecasted book gets fresh, correct scores from the live
$0 LLM sweep; every other deal keeps its swept score and just gains readable reasons.

Pinned deals are left untouched. Opp-scoped jsonb_set on ai.deal_scores.cro_panel.
Dry-run by default; --apply to write. Pass --rescore ONLY to also recompute scores
(NOT recommended outside a sweep).
"""
from __future__ import annotations
import re, json, sys
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15
import deal_engine_scoring as SC
import deal_engine_cro as CRO

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _has_analysis(ai):
    return bool(ai.get("meddpicc") or ai.get("competitive_position") or ai.get("champion_strength")
               or ai.get("recommended_moves"))


def _first_win_bullet(panel):
    for b in (panel or {}).get("blocks", []):
        if b.get("key") == "win_position" and b.get("bullets"):
            return b["bullets"][0].get("text", "")
    return ""


def main():
    apply = "--apply" in sys.argv
    rescore = "--rescore" in sys.argv     # NOT recommended: recompute scores too
    exclude = set()
    for a in sys.argv:
        if a.startswith("--exclude="):
            try:
                exclude = {id15(x) for x in json.load(open(a.split("=", 1)[1]))}
            except Exception:
                exclude = {id15(x) for x in a.split("=", 1)[1].split(",") if x}

    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    token = sec["SUPABASE_ACCESS_TOKEN"]

    rows = requests.get(f"{base}/rest/v1/deal_records",
                        params={"select": "opp_id,account_name,record", "active": "eq.true"},
                        headers={"apikey": key, "Authorization": f"Bearer {key}"},
                        verify=VERIFY, timeout=180).json()

    out = {}
    n_panel = n_changed = skip_pinned = skip_noanalysis = skip_noscore = excluded = 0
    samples = []
    for r in rows:
        oid = id15(r["opp_id"])
        if oid in exclude:
            excluded += 1; continue
        rec = r.get("record") or {}
        ai = rec.get("ai") or {}
        if ai.get("pinned"):
            skip_pinned += 1; continue
        if not _has_analysis(ai):
            skip_noanalysis += 1; continue
        ds = dict(ai.get("deal_scores") or {})
        has_head = (ds.get("headline") or {}).get("win_position") is not None
        try:
            if rescore:
                sc = SC.compute_deal_scores(rec)
                if sc and (sc.get("headline") or {}).get("win_position") is not None:
                    ds = sc; rec.setdefault("ai", {})["deal_scores"] = sc; has_head = True
            if not has_head:
                skip_noscore += 1; continue      # no score to attach a panel to — leave for a sweep
            # ensure build_cro_panel sees the (existing) scores on the record
            rec.setdefault("ai", {})["deal_scores"] = ds
            old_first = _first_win_bullet(ds.get("cro_panel"))
            panel = CRO.build_cro_panel(rec)
            if not panel:
                continue
            ds["cro_panel"] = panel
            out[oid] = ds
            n_panel += 1
            if _first_win_bullet(panel) != old_first:
                n_changed += 1
                if len(samples) < 8:
                    samples.append((r.get("account_name"), _first_win_bullet(panel)[:150]))
        except Exception as e:
            print("  failed", oid, type(e).__name__, str(e)[:120])

    print(f"active: {len(rows)} | panels rebuilt: {n_panel} | reasons changed: {n_changed} | "
          f"skip pinned: {skip_pinned} | skip no-analysis: {skip_noanalysis} | skip no-score: {skip_noscore} | "
          f"excluded: {excluded} | mode: {'RESCORE+panel' if rescore else 'panel-only (scores untouched)'}")
    if samples:
        print("\nsample new human-readable win bullets:")
        for acct, t in samples:
            print(f"   {str(acct)[:22]:22} | {t}")
    if not apply:
        print("\n[DRY RUN] pass --apply to write.")
        return

    items = list(out.items())
    total = 0
    for i in range(0, len(items), 40):
        blob = json.dumps(dict(items[i:i + 40]))
        sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), "
               "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
               "where d.opp_id = m.opp_id returning d.opp_id")
        resp = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=VERIFY, timeout=150)
        if resp.status_code >= 300:
            print("APPLY FAILED", resp.status_code, resp.text[:300]); break
        total += len(resp.json())
    print(f"\nAPPLIED: {total} panels refreshed (human-readable reasons; scores unchanged)")


if __name__ == "__main__":
    main()
