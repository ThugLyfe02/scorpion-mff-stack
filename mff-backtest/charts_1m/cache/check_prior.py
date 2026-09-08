import json
d=open("/workspace/agent-tools/114c0be2-7057-40a6-a3e3-d4e845329eee.txt").read()
print(d[:500])
i=d.find("{"); j=d.rfind("}"); obj=json.loads(d[i:j+1])
print(obj.keys())
data=obj.get("data",{})
print(data.keys() if isinstance(data,dict) else type(data))
if "results" in data:
  r=data["results"][0]; print(r.keys()); bars=r.get("bars",[]); print("nbars",len(bars)); print(bars[0] if bars else None); print(bars[100] if len(bars)>100 else None)
