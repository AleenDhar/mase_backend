import re,json
f=r"C:\Users\Aleen.Dhar\.claude\projects\C--Users-Aleen-Dhar-Downloads-Agent-Salesforce-Link--1--Agent-Salesforce-Link\7a24ea2b-aa69-4cbf-9a40-f966c1736687\tool-results\mcp-claude_ai_Deepagent_MCP-call_tool-1782749397616.txt"
raw=open(f,encoding='utf-8').read()
# unescape the JSON layers crudely: find the inner text
# records start with {"attributes". Use regex on the doubly-escaped content.
pat=re.compile(r'\\"Subject\\":(\\"(?:[^\\"]|\\.)*?\\"|null).*?\\"ActivityDate\\":(\\"[^\\"]*\\"|null).*?\\"Status\\":(\\"[^\\"]*\\"|null).*?\\"TaskSubtype\\":(\\"[^\\"]*\\"|null)',re.S)
m=pat.findall(raw)
print('matched',len(m))
for s,a,st,sub in m:
    clean=lambda x:x.replace('\\"','').strip()
    print(clean(a),'|',clean(st),'|',clean(sub),'::',clean(s))
