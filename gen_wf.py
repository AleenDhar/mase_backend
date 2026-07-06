"""Generate the CC-sweep workflow script. Builds the agent prompt from an array of
line strings joined by a newline var (String.fromCharCode(10)) so the emitted .js
has NO literal control characters (passes the workflow approval dialog) and NO
fragile escape sequences (passes the parser)."""
import json

ids = json.load(open("cc_work/_pending.json"))
W = "C:/Users/Aleen.Dhar/Downloads/Agent-Salesforce-Link (1)/Agent-Salesforce-Link/cc_work"

# Each element becomes a JS string literal; joined in-script by NL. Use ${...}-free
# concatenation via + W + / + opp +. Blank strings produce blank lines.
lines = [
    "You ARE the MASE Deal Intelligence Engine sweep agent. Your COMPLETE system prompt (canonical-record contract, rules, output schema) is in this file - read it FIRST and follow it end-to-end:",
    "@W@/system_prompt.md",
    "",
    "Then read this ONE opportunity data file (authoritative Salesforce facts, contact roles, activities, verbatim Avoma transcripts, prior record for living-memory):",
    "@W@/@OPP@.msg",
    "",
    "Produce the canonical record JSON EXACTLY per your system prompt output contract (all ai.* keys incl north_star_verdict, meddpicc, competitive_position, recommended_moves, stakeholder_map, ceo_intervention, evidence_coverage). Ground EVERY name and title in the Salesforce contact roles (never a transcript spelling); never fabricate. For competitive_position set each competitor status accurately - only mark a competitor preferred/ahead/incumbent when the BUYER actually leans that way. Emit ceo_intervention per the CEO-help rules (win>=40 floor is only eligibility; default needed:false unless the CEO is irreplaceable).",
    "",
    "FOLLOW SECTION 2.10 (deal-quality tweak pass) IN FULL. Specifically you MUST: (1) write every MEDDPICC narrative + competitive/champion summary SPECIFIC to this deal (name the person, the evidence, the source, the date - never a generic label); (2) emit ai.deal_scores_evidence = { summary: one crisp deal-specific lead line, ai_reasons: { win_position:[...], deal_momentum:[...], customer_commitment:[...], deal_risk:[...] } } where each bullet is a full specific sourced sentence, win_position LEADS with a why-this-number bullet and INCLUDES 1-2 warn risk bullets; make the reasons MATCH the score (if the champion is weak / buyer leans a rival, set champion_strength.strength / customer_preference.level / competitor status negative so the computed score reflects it); (3) if the scope has NARROWED vs the prior record/original scope, emit ai.scope_change {direction:'reduced', from, to, detail}; (4) if this account already has a Closed-Won deal (second panel / expansion), emit ai.expansion_context {prior_closed_won:true, note} and do NOT flag no-exec-access; (5) infer the economic buyer from conversation when fields are silent; treat the last EMAIL/next-step as a valid 'last conversation'.",
    "",
    "Use the Write tool to save ONLY the JSON object (no prose, no markdown fences) to:",
    "@W@/@OPP@.json",
    "",
    "Return one short line: north_star verdict + stakeholder count + ceo needed.",
]

# Build a JS expression: pieces joined by NL. @W@ -> " + W + ", @OPP@ -> " + opp + "
parts = []
for ln in lines:
    js = json.dumps(ln)                       # safe JS string literal (no ctrl chars)
    js = js.replace("@W@", '" + W + "').replace("@OPP@", '" + opp + "')
    parts.append(js)
prompt_expr = " + NL + ".join(parts)

idlist = ", ".join(json.dumps(i) for i in ids)

script = "".join([
    "export const meta = {\n",
    "  name: 'cc-sweep-full',\n",
    "  description: 'Claude-Code deal sweep for all " + str(len(ids)) + " pending opps',\n",
    "  phases: [{ title: 'Sweep', detail: 'one agent per deal' }],\n",
    "}\n",
    "const NL = String.fromCharCode(10)\n",
    "const W = " + json.dumps(W) + "\n",
    "const IDS = [" + idlist + "]\n",
    "phase('Sweep')\n",
    "const results = (await parallel(IDS.map((opp) => () =>\n",
    "  agent(" + prompt_expr + ", { phase: 'Sweep', label: 'sweep:' + opp, effort: 'medium' })\n",
    "))).filter(Boolean)\n",
    "return { swept: results.length }\n",
])

open("cc_work/_sweep_wf.js", "w", encoding="utf-8", newline="\n").write(script)
stray = [c for c in script if ord(c) < 32 and c != "\n"]
print("wrote", len(script), "chars,", len(ids), "agents | stray control chars:", len(stray))
