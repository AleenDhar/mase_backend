"""Wrapper: hydrate env from the cached secret (kills the AWS/Zscaler stall), then run
day_summary_ai's CLI. Passes through argv after the wrapper name."""
import sys, boot_env
boot_env.hydrate(verbose=True)
import day_summary_ai
sys.argv = ["day_summary_ai.py"] + sys.argv[1:]
day_summary_ai.main()
