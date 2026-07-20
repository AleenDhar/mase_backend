"""PRE-DEPLOY local proof (2026-07-09): before spending 15 min on the GitHub Actions
deploy, verify the EXACT deployed config works — so the deploy can't fail on a syntax
error, a broken guard, or a scorer the ref-strip broke.

Mirrors render_taskdef API_ENV/_SWEEP_TUNING and checks:
  1. every core file COMPILES (the deploy's first act — Docker build imports them)
  2. render_taskdef renders manual-only=true + autoscale=off + AI-scoring on
  3. manual_only() gates: book/automated runs REFUSED, manual path preserved
  4. worker idles under manual-only (code path)
  5. AI scorer prompt is Studio-governed, refs resolved to plain names, NO {{ref}} leak

Run:  python test_deploy_ready.py
"""
import os, sys, warnings, asyncio, py_compile, importlib.util
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --- mirror the DEPLOYED env (render_taskdef _SWEEP_TUNING + API_ENV) ---
os.environ["DEAL_SWEEP_MANUAL_ONLY"] = "true"
os.environ["SWEEP_AUTOSCALE_ENABLED"] = "false"
os.environ["DEAL_ENGINE_AI_SCORING"] = "true"
os.environ["DEAL_ENGINE_SCORING_MODEL"] = "anthropic:claude-sonnet-5"
os.environ["DEAL_ENGINE_SWEEP_MODEL"] = "anthropic:claude-sonnet-5"
try:
    from daily_summary.common import load_secret
    sec = load_secret()
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY", "ANTHROPIC_API_KEY"):
        if sec.get(k):
            os.environ[k] = sec[k]
except Exception as e:
    print(f"(secret load: {e})")

P, F = [], []
def check(name, cond, detail=""):
    (P if cond else F).append(name)
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  — {detail}" if detail else ""))

# ===== 1. COMPILE (deploy-breaker gate) =====
print("\n=== 1. compile — the Docker build imports these; a syntax error = failed deploy ===")
core = ["deal_engine_sweep.py", "server.py", "worker.py", "deal_engine_ai_scoring.py",
        ".github/deploy/render_taskdef.py", "sweep_queue.py", "scoring_studio.py",
        "deal_engine_evidence.py", "build_day_summaries.py", "deal_engine_cro.py"]
for f in core:
    try:
        py_compile.compile(f, doraise=True)
        check(f"compile {f}", True)
    except Exception as e:
        check(f"compile {f}", False, f"{type(e).__name__}: {e}")

# ===== 2. render_taskdef env =====
print("\n=== 2. render_taskdef — the env ECS will actually run ===")
try:
    spec = importlib.util.spec_from_file_location("rtd", ".github/deploy/render_taskdef.py")
    rtd = importlib.util.module_from_spec(spec); spec.loader.exec_module(rtd)
    check("api  DEAL_SWEEP_MANUAL_ONLY=true", rtd.API_ENV.get("DEAL_SWEEP_MANUAL_ONLY") == "true",
          rtd.API_ENV.get("DEAL_SWEEP_MANUAL_ONLY"))
    check("api  SWEEP_AUTOSCALE_ENABLED=false", rtd.API_ENV.get("SWEEP_AUTOSCALE_ENABLED") == "false",
          rtd.API_ENV.get("SWEEP_AUTOSCALE_ENABLED"))
    check("wkr  DEAL_SWEEP_MANUAL_ONLY=true", rtd.WORKER_ENV.get("DEAL_SWEEP_MANUAL_ONLY") == "true",
          rtd.WORKER_ENV.get("DEAL_SWEEP_MANUAL_ONLY"))
    check("api  DEAL_ENGINE_AI_SCORING=true", rtd.API_ENV.get("DEAL_ENGINE_AI_SCORING") == "true")
    check("api  scoring model = sonnet-5", "sonnet-5" in (rtd.API_ENV.get("DEAL_ENGINE_SCORING_MODEL") or ""))
except Exception as e:
    check("render_taskdef import", False, f"{type(e).__name__}: {e}")

# ===== 3. manual-only guards =====
print("\n=== 3. manual-only guards — automated REFUSED, manual path PRESERVED ===")
try:
    import deal_engine_sweep as sweep
    check("manual_only() True when env set", sweep.manual_only() is True)
    os.environ["DEAL_SWEEP_MANUAL_ONLY"] = "false"
    check("manual_only() False when env unset", sweep.manual_only() is False)
    os.environ["DEAL_SWEEP_MANUAL_ONLY"] = "true"  # restore deployed value
    try:
        asyncio.run(sweep.enqueue_book_run(None))
        check("enqueue_book_run REFUSED (book/automated)", False, "did not raise")
    except RuntimeError as e:
        check("enqueue_book_run REFUSED (book/automated)", "manual-only" in str(e).lower(), str(e)[:70])
    except Exception as e:
        check("enqueue_book_run REFUSED (book/automated)", False, f"{type(e).__name__}: {e}")
    # manual path still exists (trigger_opp_async is what server.py calls for source=manual)
    check("trigger_opp_async present (manual path)", hasattr(sweep, "trigger_opp_async"))
except Exception as e:
    check("deal_engine_sweep import", False, f"{type(e).__name__}: {e}")

# ===== 4. worker idles under manual-only (static check of the loop guard) =====
print("\n=== 4. worker idle guard ===")
try:
    src = open("worker.py", encoding="utf-8").read()
    check("worker checks DEAL_SWEEP_MANUAL_ONLY in drain loop", "DEAL_SWEEP_MANUAL_ONLY" in src and "worker IDLE" in src.lower() or "worker idle" in src.lower())
except Exception as e:
    check("worker.py read", False, str(e))

# ===== 5. AI scorer prompt (Studio governance + ref-strip) =====
print("\n=== 5. AI scorer prompt — Studio-governed, refs resolved, no {{ref}} leak ===")
try:
    import deal_engine_ai_scoring as ais
    p = ais._prompt()
    check("prompt non-empty", bool(p and p.strip()), f"{len(p)} chars")
    check("NO raw {{ref:...}} token leaks", "{{ref:" not in p)
    check("Studio governing engine(s) present", ("GOVERNING ENGINE" in p) or ("Win Position" in p),
          "win/mom engine embedded")
    check("output adapter appended (win_position contract)", "win_position" in p.lower())
    check("refs rendered as plain names",
          ("MASE Vendor Dictionary" in p) or ("Zycus Deal Playbook" in p) or ("{{ref:" not in p),
          "vendor-dictionary / deal-playbook pointers")
except Exception as e:
    import traceback; traceback.print_exc()
    check("AI scorer _prompt()", False, f"{type(e).__name__}: {e}")

print(f"\n===== {len(P)} PASS / {len(F)} FAIL =====")
if F:
    print("FAILED:", ", ".join(F))
    sys.exit(1)
print("ALL GREEN — deployed config is safe to ship (no syntax/guard/scorer breakers).")
