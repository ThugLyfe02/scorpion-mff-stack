import json
d=open("/workspace/agent-tools/3b6e3f8d-0e7f-4f60-92dc-8883e402c6c8.txt").read()
i=d.find("{"); j=d.rfind("}"); obj=json.loads(d[i:j+1])
r=obj["data"]["results"][0]
bars=r["bars"]
closes=[float(b["close_price"]) for b in bars]
print("n",len(bars),"uniq",len(set(closes)),"min",min(closes),"max",max(closes))
real=[b for b in bars if not b.get("interpolated")]
print("non_interp",len(real))
changing=[b for b in bars if float(b["open_price"])!=float(b["close_price"]) or float(b["high_price"])!=float(b["low_price"])]
print("ohlc_vary",len(changing))
print("first3",bars[:3])
print("mid",bars[len(bars)//2])
print("sample_changing",changing[:2] if changing else "none")
