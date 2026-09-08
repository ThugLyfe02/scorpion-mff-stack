import json
d=open("/workspace/agent-tools/114c0be2-7057-40a6-a3e3-d4e845329eee.txt").read()
i=d.find("{"); j=d.rfind("}"); obj=json.loads(d[i:j+1])
for r in obj["data"]["results"][:3]:
  bars=r["bars"]
  closes=[float(b["close_price"]) for b in bars]
  real=[b for b in bars if not b.get("interpolated")]
  print(r["occ_symbol"].strip(), r["interval"], r["bounds"], "n",len(bars),"real",len(real),"uniq",len(set(closes)),"min",min(closes),"max",max(closes))
  if real: print(" real0", real[0]); print(" real50", real[min(50,len(real)-1)])
