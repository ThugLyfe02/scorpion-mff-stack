import json, csv
from datetime import datetime, timedelta
from collections import defaultdict

trades=json.load(open('/workspace/mff-backtest/bt_full/trades.json'))
R=500.0
BASE=R/0.25
ADD=0.5*R/0.25
START=5000.0

def parse_t(t):
    d,tm=t['date'], t['time_et']
    s=tm.strip().upper().replace('.','')
    for fmt in ('%H:%M','%I:%M %p','%I:%M%p','%H:%M:%S'):
        try:
            tt=datetime.strptime(s, fmt).time()
            return datetime.fromisoformat(d).replace(hour=tt.hour, minute=tt.minute)
        except Exception:
            pass
    return datetime.fromisoformat(d)

rows=[t for t in trades if t['entry_bid'] is not None and t['exit_pct'] is not None]
rows.sort(key=lambda t: (t['date'], t['time_et']))

def sim(with_add=False, max_pos=2, assume_add_always=True):
    book=START
    open_pos=[]
    taken=[]; skipped=[]
    equity=[{'i':0,'book':book,'pnl':0,'label':'start'}]
    for t in rows:
        bid=float(t['entry_bid']); pct=float(t['exit_pct'])
        entry_dt=parse_t(t)
        notes=(t.get('notes') or '').lower()
        is_0dte = (t['expiry']==t['date'])
        try:
            exp=datetime.fromisoformat(t['expiry'])
            multi = exp.date() > datetime.fromisoformat(t['date']).date()
        except Exception:
            multi=False
        if is_0dte:
            close_dt = entry_dt.replace(hour=16, minute=0)
        elif multi:
            close_dt = entry_dt + timedelta(days=3)
        else:
            close_dt = entry_dt.replace(hour=16, minute=0)

        open_pos=[p for p in open_pos if p[0] > entry_dt]
        if len(open_pos) >= max_pos:
            skipped.append({'date':t['date'],'ticker':t['ticker'],'reason':'max2'})
            continue

        qty1=int(BASE // (bid*100))
        if qty1<1:
            skipped.append({'date':t['date'],'ticker':t['ticker'],'reason':'size'})
            continue
        qty=qty1
        avg=bid
        added=False
        will_add = with_add and assume_add_always and pct != 0
        if with_add and any(k in notes for k in ('added','avg ','average')):
            will_add=True
        if will_add:
            add_px=bid*0.85
            qty2=int(ADD // (add_px*100))
            if qty2>=1 and (qty1*bid*100 + qty2*add_px*100) <= (BASE+ADD)+1:
                qty+=qty2
                avg=(qty1*bid + qty2*add_px)/qty
                added=True

        exit_px = bid * (1 + pct/100.0)
        pnl = qty * (exit_px - avg) * 100
        book += pnl
        label=f"{t['date']} {t['ticker']} {t['side'][0].upper()}{t['strike']} {pct:+.0f}%"
        taken.append({
            'date':t['date'],'ticker':t['ticker'],'side':t['side'],'strike':t['strike'],
            'expiry':t['expiry'],'channel':t['channel'],'bid':bid,'pct':pct,'qty':qty,
            'avg':round(avg,4),'exit':round(exit_px,4),'added':added,'pnl':round(pnl,2),
            'book':round(book,2),'R':round(pnl/R,2),'outcome':t['outcome'],
            'notes':(t.get('notes') or '')[:100]
        })
        open_pos.append((close_dt, label))
        equity.append({'i':len(taken),'book':round(book,2),'pnl':round(pnl,2),'label':label,'date':t['date']})

    pnls=[x['pnl'] for x in taken]
    wins=sum(1 for p in pnls if p>0)
    peak=START; maxdd=0
    for e in equity:
        cur=e['book']; peak=max(peak,cur); maxdd=min(maxdd, cur-peak)
    monthly=defaultdict(float)
    for x in taken:
        monthly[x['date'][:7]] += x['pnl']
    return {
        'end': round(book,2), 'net': round(book-START,2), 'n': len(taken), 'skipped': len(skipped),
        'wr': round(wins/len(taken)*100,1) if taken else 0, 'wins':wins,
        'losses': sum(1 for p in pnls if p<=0),
        'avg_R': round(sum(pnls)/len(pnls)/R, 2) if pnls else 0,
        'best': max(taken, key=lambda x:x['pnl']) if taken else None,
        'worst': min(taken, key=lambda x:x['pnl']) if taken else None,
        'maxdd': round(maxdd,2),
        'monthly': {k: round(v,2) for k,v in sorted(monthly.items())},
        'taken': taken, 'skipped_list': skipped,
        'adds': sum(1 for x in taken if x['added']),
    }

A=sim(True,2); B=sim(False,2); A0=sim(True,99); B0=sim(False,99)

def brief(name,S):
    print(f"\n=== {name} ===")
    print(f"n={S['n']} skip={S['skipped']} WR={S['wr']}% adds={S['adds']}")
    print(f"NET ${S['net']:+,.0f} END ${S['end']:,.0f} avgR {S['avg_R']:+.2f} maxDD ${S['maxdd']:,.0f}")
    b,w=S['best'],S['worst']
    if b: print(f"BEST {b['date']} {b['ticker']} {b['pct']:+.0f}% ${b['pnl']:+,.0f}")
    if w: print(f"WORST {w['date']} {w['ticker']} {w['pct']:+.0f}% ${w['pnl']:+,.0f}")
    print('Monthly', S['monthly'])

brief('A add max2',A); brief('B noadd max2',B)
brief('A add NO max (multi OK)',A0); brief('B noadd NO max',B0)

from collections import Counter
dayc=Counter(t['date'] for t in rows)
print('\nMulti-signal days', sum(1 for d,c in dayc.items() if c>1))
print('Top', dayc.most_common(10))
# swings: expiry > date
sw=[t for t in rows if t['expiry']!=t['date']]
print('Non-0DTE / swing-ish rows', len(sw))

out={
 'assumptions':'Numbers-first from Discord exit_pct. Entry=bid. Exit=bid*(1+pct/100) as effective full-clip %. Add assumes -15% fill once when enabled. Concurrency by approx hold to 4pm 0DTE or +3d multi-expiry. Not bar-validated.',
 'params':{'start':START,'1R':R,'base_premium':BASE,'add_premium':ADD},
 'A_add_max2':{k:v for k,v in A.items() if k not in ('taken','skipped_list')},
 'B_noadd_max2':{k:v for k,v in B.items() if k not in ('taken','skipped_list')},
 'A_add_nomax':{k:v for k,v in A0.items() if k not in ('taken','skipped_list')},
 'B_noadd_nomax':{k:v for k,v in B0.items() if k not in ('taken','skipped_list')},
 'excluded_no_exit_pct': sum(1 for t in trades if t['exit_pct'] is None),
 'multi_signal_days': sum(1 for d,c in dayc.items() if c>1),
}
json.dump(out, open('/workspace/mff-backtest/bt_full/numbers_first_summary.json','w'), indent=2)
with open('/workspace/mff-backtest/bt_full/numbers_first_add_nomax.csv','w',newline='') as f:
    w=csv.DictWriter(f, fieldnames=list(A0['taken'][0].keys())); w.writeheader(); w.writerows(A0['taken'])
with open('/workspace/mff-backtest/bt_full/numbers_first_add_max2.csv','w',newline='') as f:
    w=csv.DictWriter(f, fieldnames=list(A['taken'][0].keys())); w.writeheader(); w.writerows(A['taken'])
print('saved')
