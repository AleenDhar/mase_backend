# To-do bucket hygiene (dedup + cross-bucket de-collision)

The four UI to-do buckets (Prospect requirements / Next phase / Waiting on the buyer /
Best practices) are **projected from packets** in `project_into_ai`
(`deal_engine_packets.py`): one `commitment` packet → one `open_deliverable`, one
`hygiene` packet → one `best_practice` flag. Under carry-forward the model re-lists the
same live thread many times, both **within** a block and **across** the three blocks that
feed the buckets — so the surface bloats with duplicates.

**Where dedup lives.** `todo_grouping.tidy()` is called **at the end of `project_into_ai`**
(the single projection chokepoint), NOT only as a post-sweep step in `analyze_one`. This is
deliberate: the lists are rebuilt from packets on every projection, so a dedup bolted on
*after* projection silently vanishes on any re-projection (that bug showed as `group_key=None`
on every served item — the grouper had run somewhere but its output never reached persist).
Putting it in the projection makes the buckets clean **by construction**.

`tidy()` = `group_todo_lists()` (within-block homogeneous merge by token-set overlap) +
`decollide_buckets()` (cross-bucket: drop a `best_practice` flag that restates a
`recommended_move`/`open_deliverable`; keep true action-less gaps). Conservative — needs ≥2
shared content tokens and ≥0.55 overlap of the action's signature, so it trims repeats without
emptying buckets (dark deals keep their gaps).

**Invariants.** Packets are never mutated (full living-memory history preserved); only the
projected display lists are tidied → safe + re-runnable + idempotent. Existing records clean
on next sweep, or via a token-free re-projection pass (load record →
`project_into_ai(rec['ai'], rec['packets'])` → re-store; no Avoma/SF). Knobs:
`todo_grouping._DECOLLIDE_THRESHOLD` and the per-block thresholds in `_group_*`.

See [[deal-living-memory]].
