---
name: Salesforce 15- vs 18-char IDs
description: Why joining report-export opp IDs to live Salesforce query results needs prefix matching.
---

When joining IDs from a Salesforce **report export** (HTML/.xls or CSV) to IDs
returned by the **Salesforce API/SOQL**, match on the **first 15 characters**, not
the full string.

**Why:** Report exports commonly emit 15-char (case-sensitive) record IDs, while
the API returns the 18-char (case-insensitive, checksum-suffixed) form. The first
15 chars are identical; an exact-string join silently misses every row (looks like
"enrichment returned nothing" with no error).

**How to apply:** Key both sides on `id[:15]` when correlating. Seen in the Deal
Engine sweep's `_enrich_opp_ids` (label lookup for an explicit opp_id list from a
filtered report) — the bare-fallback path made it look like the SOQL failed when it
was really a 15-vs-18 key mismatch.
