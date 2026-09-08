#!/usr/bin/env python3
"""python3 size_whatif.py 1500  → scales from grid / $1200"""
import json,sys
from pathlib import Path
g=json.load(open(Path(__file__).parent/"size_grid.json"))
prem=float(sys.argv[1]) if len(sys.argv)>1 else 1200
rows=g["grid"]
exact=[r for r in rows if abs(r["prem"]-prem)<1e-6]
if exact:
    r=exact[0]
else:
    b=next(r for r in rows if r["prem"]==1200)
    sc=prem/1200.0
    r={k:(b[k]*sc if isinstance(b[k],(int,float)) and k not in ("prem","base_wr","base_taken") else b[k]) for k in b}
    r["prem"]=prem; r["add_cap"]=prem*1.5; r["daily_brake"]=-prem
    r["note"]="linear scale from $1200 (not re-simulated)"
print(f"${r['prem']:.0f}/clip | add≤${r.get('add_cap',prem*1.5):.0f} | day brake ${r.get('daily_brake',-prem):.0f}")
print(f"  base  ~${r['base_avg_mo']:,.0f}/mo  (net ${r['base_net']:,.0f})")
print(f"  harsh ~${r['harsh_avg_mo']:,.0f}/mo")
print(f"  perfect ~${r['perfect_avg_mo']:,.0f}/mo")
