import warnings; warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
import db_backup as B
CHK=["deal_records","scoring_instructions","mase_chats","deal_trigger_runs","deal_daily_summaries",
     "field_history_cache","mcp_tool_outputs","deal_todo_pushes","sweep_learnings","mase_documents"]
print(f"{'table':30}{'main':>10}{'backup':>10}  match")
print("-"*54)
allok=True
for t in CHK:
    m=B.count(B.MAIN_URL,B.MAIN_H,t)
    b=B.count(B.BAK_URL,B.BAK_H,t)
    ok=b>=m
    allok=allok and ok
    print(f"{t:30}{m:>10,}{b:>10,}  {'OK' if ok else 'BEHIND'}")
print("-"*54); print("ALL MATCH" if allok else "SOME BEHIND")
