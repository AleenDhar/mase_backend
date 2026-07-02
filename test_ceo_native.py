"""Unit test for deal_engine_ceo.finalize_ceo_intervention — the native CEO-help
gate + sanitize. No network. Run: python test_ceo_native.py"""
import sys, json
import deal_engine_ceo as C
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BUYER = {"contacts": [{"name": "Engelbert Pölki", "title": "Manager Strategic Projects"},
                      {"name": "Karin Eppich", "title": "Purchasing processes"}]}


def rec(win, mom, ci=None, md=None):
    ai = {"deal_scores": {"headline": {"win_position": win, "deal_momentum": mom}}}
    if ci is not None:
        ai["ceo_intervention"] = ci
    if md is not None:
        ai["meddpicc"] = {"economic_buyer": md}
    return {"ai": ai, "hard": {"amount": 448680}}


def main():
    ok = True

    # 1) eligible + AI says CEO NEEDED + LLM wrote "CFO Flandorfer" + real EB in MEDDPICC
    r = rec(62, 64,
            ci={"needed": True, "areas": ["exec_connect", "pricing"], "priority": "high",
                "reason": "the economic-buyer CFO Flandorfer never engaged",
                "ceo_action": "CEO connects to CFO Flandorfer to approve pricing",
                "buyer_target": {"name": "Herr Flandorfer", "title": "CFO", "engaged": False},
                "lower_execs_engaged": [{"name": "Amit Shah", "title": "CMO"}]},
            md={"name": "Barbara Potisk-Eibensteiner", "title": "CFO"})
    C.finalize_ceo_intervention(r, {"forecast_category": "Upside Key Deal"}, BUYER)
    ci = r["ai"]["ceo_intervention"]
    blob = json.dumps(ci)
    t1 = (ci["needed"] is True and ci["source"] == "sweep"
          and "CFO Flandorfer" not in blob
          and ci["buyer_target"]["name"] == "Barbara Potisk-Eibensteiner")
    print(f"[{'PASS' if t1 else 'FAIL'}] 1 gate-pass: strip title + repair buyer_target -> {ci['buyer_target']['name']}")
    ok &= t1

    # 2) NOT forecasted -> needed False regardless of high scores
    r = rec(90, 90, ci={"areas": ["pricing"], "ceo_action": "x"})
    C.finalize_ceo_intervention(r, {"forecast_category": "Pipeline"}, BUYER)
    t2 = r["ai"]["ceo_intervention"]["needed"] is False
    print(f"[{'PASS' if t2 else 'FAIL'}] 2 non-forecasted -> needed False")
    ok &= t2

    # 3) forecasted but WIN below bar -> needed False (even with high momentum)
    r = rec(58, 80, ci={"areas": ["pricing"], "ceo_action": "x"})
    C.finalize_ceo_intervention(r, {"forecast_category": "Commit"}, BUYER)
    t3 = r["ai"]["ceo_intervention"]["needed"] is False
    print(f"[{'PASS' if t3 else 'FAIL'}] 3 win=58 below bar -> needed False (momentum ignored)")
    ok &= t3

    # 3b) winnable but STALLING (win>60, low momentum) + AI says CEO needed -> needed True
    r = rec(65, 40, ci={"needed": True, "areas": ["exec_connect"], "ceo_action": "CEO steps in to un-stall"})
    C.finalize_ceo_intervention(r, {"forecast_category": "Best Case"}, BUYER)
    t3b = r["ai"]["ceo_intervention"]["needed"] is True
    print(f"[{'PASS' if t3b else 'FAIL'}] 3b win=65 stalling + AI-yes -> needed True")
    ok &= t3b

    # 3c) floor PASSES (win 80) but AI says NO CEO (a VP suffices) -> needed False
    r = rec(80, 80, ci={"needed": False, "reason": "a VP can handle the CFO connect"})
    C.finalize_ceo_intervention(r, {"forecast_category": "Commit"}, BUYER)
    t3c = r["ai"]["ceo_intervention"]["needed"] is False
    print(f"[{'PASS' if t3c else 'FAIL'}] 3c win=80 but AI-says-no-CEO -> needed False (the discriminator)")
    ok &= t3c

    # 4) passes but LLM emitted nothing + prior exists -> carry prior forward
    prior = {"ceo_intervention": {"needed": True, "areas": ["exec_connect"], "priority": "high",
             "ceo_action": "CEO engages CFO Barbara Potisk-Eibensteiner",
             "buyer_target": {"name": "Barbara Potisk-Eibensteiner", "title": "CFO"}}}
    r = rec(70, 70)
    C.finalize_ceo_intervention(r, {"forecast_category": "Best Case"}, BUYER, prior_ai=prior)
    ci = r["ai"]["ceo_intervention"]
    t4 = ci["needed"] is True and bool(ci["ceo_action"])
    print(f"[{'PASS' if t4 else 'FAIL'}] 4 no-LLM-content -> carry prior: {ci['ceo_action'][:40]}…")
    ok &= t4

    # 5) areas clamped to the 4 CEO levers (drop junk)
    r = rec(80, 80, ci={"needed": True, "areas": ["pricing", "send_a_vp", "exec_connect"], "ceo_action": "CEO acts"})
    C.finalize_ceo_intervention(r, {"forecast_category": "Commit"}, BUYER)
    t5 = r["ai"]["ceo_intervention"]["areas"] == ["pricing", "exec_connect"]
    print(f"[{'PASS' if t5 else 'FAIL'}] 5 areas clamped to CEO levers -> {r['ai']['ceo_intervention']['areas']}")
    ok &= t5

    print("\nALL PASS" if ok else "\nSOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
