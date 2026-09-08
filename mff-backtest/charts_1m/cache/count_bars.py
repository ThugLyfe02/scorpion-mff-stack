import json,os
base="/workspace/agent-tools"
files=["c7926c23-b6a5-4bfe-b1be-1f03583a1ceb.txt","5c912ea6-2fd6-46f9-a078-392645229e51.txt","3bb697e1-7404-4ec9-998c-5ef45e010443.txt","9553cbe5-8007-4425-930d-36a6fd710a7d.txt","bc5901b7-9d2d-4285-973e-d2b6759958cf.txt","ad7a4be6-9cef-4f88-a0ef-05efc028335f.txt","f9d1ee1b-e3fa-41c4-903c-c4a27cd8e05d.txt","317b1828-ba78-4652-a452-b67937ff1758.txt","3e570ad9-1609-49a5-9ab5-6641a2b33dbb.txt","78f5af61-63a2-40f9-9cec-31a8d3fc8c83.txt"]
for fn in files:
    d=open(os.path.join(base,fn)).read()
    i=d.find("{"); j=d.rfind("}")
    obj=json.loads(d[i:j+1])
    for r in obj["data"]["results"]:
        bars=r["bars"]
        real=[b for b in bars if not b.get("interpolated")]
        sym=r["symbol"]; occ=r["occ_symbol"].strip()
        fr=real[0]["begins_at"] if real else None
        px=real[0]["close_price"] if real else None
        print(sym, occ, "total", len(bars), "real", len(real), "first", fr, "px", px)
