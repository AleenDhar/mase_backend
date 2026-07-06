"""Generate the CEO-attention judge workflow (one agent per win>=40 pack). LF-only,
no literal control chars in strings (String.fromCharCode(10) for newlines)."""
import json, os

idx = json.load(open("ceo_attention/_index.json"))
ids = [x["opp_id"] for x in idx]
W = "C:/Users/Aleen.Dhar/Downloads/Agent-Salesforce-Link (1)/Agent-Salesforce-Link/ceo_attention"

RULES = [
 "You decide CEO ATTENTION for a Zycus enterprise deal (win_position>=40). TWO separate determinations:",
 "",
 "A) SUPPORT - the CEO must personally ACT (his availability / veto). Levers: pricing (approve a blocked discount/commercial flexibility), product (commit roadmap), presales_resources (guarantee SE/POC/implementation), exec_connect (CEO-to-buyer-CEO/CFO/CPO peer relationship). DEFAULT needed:false. Set support.needed:true ONLY if the CEO is IRREPLACEABLE - a VP/SVP/CRO could NOT do it. Give why_not_vp.",
 "",
 "B) MONITOR - the CEO should WATCH (awareness, to gauge that WE are slipping). Three triggers, and EVERY monitor flag MUST be anchored to a signal dated within the LAST 14 DAYS (cite it in evidence + as_of). If your only support for a flag is older than 14 days, DO NOT raise it - being surgical and recent matters most.",
 "  T1 our_slip: a deliverable the PROSPECT expected from US (see our_open_deliverables) is still outstanding/unmoved AND we are NOT blocked on the buyer. GO SOFT / do NOT raise it when it is buyer_dependent (we are waiting on the buyer for info) - that is not our lapse. Anchor to a <=14-day signal (a recent buyer chase/ask, or recent activity showing no progress from us).",
 "  T2 large_slowdown: the deal is large (is_large=amount>=250k) OR forecasted AND it is slowing or disengaging - a close-date pushed out in recent_movements_14d, momentum low, or days_since_last_activity high (>14 = gone quiet). Must cite a <=14-day signal.",
 "  T3 competitor_edge: our recent interactions (recent_calls_14d / competitive_position) show a competitor doing something BETTER than Zycus (delivery, capability, responsiveness). Cite the <=14-day call/quote.",
 "",
 "IGNORE CRM HOUSEKEEPING as movement: Owner/Co-owner reassignment, Type/Probability/Opportunity_Source edits, field cleanups - these are admin, NEVER a monitor trigger.",
 "A deal can be BOTH (a real lapse on our side = monitor AND support). If neither fires, kind=none, needed=false.",
 "Ground everything in the pack; never invent names/quotes. buyer_target names come from meddpicc_economic_buyer / champion_strength (Salesforce), never a transcript.",
 "",
 "DEPTH - each reason must be RICH and self-contained so a CEO grasps it in 10 seconds WITHOUT opening the deal. For EVERY trigger/support give:",
 "  - summary: one sharp CEO-facing headline (<=15 words).",
 "  - detail: 2-4 full sentences with the SPECIFICS - what exactly is happening, since when, the dollars/stage at stake, and the CONSEQUENCE if ignored (what we lose / the risk).",
 "  - metric: the single hardest number that proves it (e.g. '25 days no buyer activity', 'close date pushed 31 days (Jun 30 -> Jul 31)', 'POC 0 of 5 use cases', '$1.5M ARR').",
 "  - owner: the Zycus deal owner/RSD accountable (from pack 'owner') - who the CEO would ask.",
 "  - ceo_ask: the concrete thing the CEO should DO or ASK. For a WATCH reason this is a pointed question the CEO puts to the VP (pack field 'vp' = the owner's MANAGER) - NOT the rep/BDR. The CEO talks to his VP, who owns the rep's book; he does not chase the individual rep. NAME THE VP, and keep the rep named for context so pronouns resolve ('Ask <vp> (VP over <owner>) why the Ariba beta result is 25 days overdue and whether Gaurav has gone cold'). If pack 'vp' is null, address 'the deal owner's manager'. For a SUPPORT reason it is the CEO's own action.",
 "Be specific and concrete - name the person, the deliverable, the competitor, the date. No vague filler.",
]

lines = RULES + [
 "",
 "STEP 1: Read this deal's 14-day evidence pack with the Read tool:",
 "@W@/@OPP@.json",
 "",
 "STEP 2: Decide SUPPORT and MONITOR per the rules for opp_id @OPP@.",
 "",
 "STEP 3: Use the Write tool to save ONLY this JSON object (no prose/fences) to @W@/@OPP@.verdict.json :",
 '{ "opp_id":"@OPP@", "needed":<support.needed OR monitor.needed>, "kind":"support"|"monitor"|"both"|"none",',
 '  "support":{"needed":bool,"priority":"high"|"medium","areas":[...],"summary":"headline","detail":"2-4 sentences with specifics + consequence","metric":"the hard number","owner":"RSD name","ceo_action":"the CEO personal action","ceo_ask":"what the CEO does/asks","buyer_target":{"name","title"},"why_not_vp":"..."},',
 '  "monitor":{"needed":bool,"reason":"one line why the CEO should watch","triggers":[{"type":"our_slip"|"large_slowdown"|"competitor_edge","severity":"high"|"medium","summary":"headline <=15 words","detail":"2-4 sentences: specifics, since when, $ at stake, consequence if ignored","metric":"the single hardest number","owner":"RSD name","ceo_ask":"pointed question the CEO asks the rep/RSD","evidence":"the grounded fact/quote","as_of":"YYYY-MM-DD within 14 days"}]},',
 '  "source":"attention_v1" }',
 "",
 "Return one line: SUPPORT yes/no + MONITOR yes/no + triggers.",
]

parts = []
for ln in lines:
    js = json.dumps(ln).replace("@W@", '" + W + "').replace("@OPP@", '" + opp + "')
    parts.append(js)
prompt_expr = " + NL + ".join(parts)
idlist = ", ".join(json.dumps(i) for i in ids)

script = "".join([
    "export const meta = {\n",
    "  name: 'ceo-attention-judge',\n",
    "  description: 'CEO attention (Support + 14-day-surgical Monitor) for all " + str(len(ids)) + " win>=40 opps',\n",
    "  phases: [{ title: 'Judge', detail: 'one agent per deal' }],\n",
    "}\n",
    "const NL = String.fromCharCode(10)\n",
    "const W = " + json.dumps(W) + "\n",
    "const IDS = [" + idlist + "]\n",
    "phase('Judge')\n",
    "const results = (await parallel(IDS.map((opp) => () =>\n",
    "  agent(" + prompt_expr + ", { phase: 'Judge', label: 'ceo:' + opp, effort: 'medium' })\n",
    "))).filter(Boolean)\n",
    "return { judged: results.length }\n",
])
open("ceo_attention/_judge_wf.js", "w", encoding="utf-8", newline="\n").write(script)
stray = [c for c in script if ord(c) < 32 and c != "\n"]
print("wrote", len(script), "chars,", len(ids), "agents | stray control chars:", len(stray))
