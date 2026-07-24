import json, numpy as np
from datetime import datetime

ROWS=[]
for line in open('trades/dataset.jsonl'):
    line=line.strip()
    if not line: continue
    d=json.loads(line)
    traj=d.get('early_traj'); o=d.get('outcome'); s15=d.get('snap_15'); s30=d.get('snap_30')
    if not(traj and o and o.get('path') and s15 and s30): continue
    ROWS.append(d)
print('full-feature rows:', len(ROWS))

def price_at(traj, t):
    p=1.0
    for tr,mult in traj:
        if tr<=t: p=mult
        else: break
    return p

# precompute per-row derived fields
def prep(d):
    traj=d['early_traj']; o=d['outcome']; s15=d['snap_15']; s30=d['snap_30']
    entry=price_at(traj,0.8)
    if entry<=0: entry=1e-9
    ret30=s30['ret_from_first']
    conv=1.0+ret30   # multiply path_mult -> x-first
    # x-first path with absolute time = 30 + t_rel
    xf=[(30.0+t, m*conv) for t,m in o['path']]
    return entry, conv, xf, ret30

DERIV=[prep(d) for d in ROWS]
TS=[datetime.fromisoformat(d['created_ts']).timestamp() for d in ROWS]

# baseline: exit at 15s (x-first = 1+snap_15.ret)
def baseline_ret(i):
    d=ROWS[i]; entry=DERIV[i][0]
    exit_xf=1.0+d['snap_15']['ret_from_first']
    return exit_xf/entry-1-0.03

# path-based exit simulation
def path_at(xf, t):
    # last price with time<=t
    p=xf[0][1]
    for tt,pp in xf:
        if tt<=t: p=pp
        else: break
    return p

def sim(i, trail=None, tp=None, tstop=None):
    # returns real scalp return applying exit rule on path (from 30s)
    entry, conv, xf, ret30 = DERIV[i]
    if not xf:
        exit_xf=1.0+ROWS[i]['snap_30']['ret_from_first']
        return exit_xf/entry-1-0.03
    runmax=-1e9
    tp_target = entry*tp if tp else None
    exit_xf=None
    for t,p in xf:
        if p>runmax: runmax=p
        # TP check
        if tp_target and p>=tp_target:
            exit_xf=p; break
        # trailing check
        if trail is not None and runmax>0 and p<=runmax*(1-trail):
            exit_xf=p; break
        # time stop
        if tstop is not None and t>=tstop:
            exit_xf=p; break
    if exit_xf is None:
        exit_xf=xf[-1][1]  # ride to end
    return exit_xf/entry-1-0.03

def perfect(i):
    entry,conv,xf,ret30=DERIV[i]
    mx=max(p for _,p in xf) if xf else (1.0+ret30)
    return mx/entry-1-0.03

# temporal split
order=np.argsort(TS)
n=len(order); half=n//2
train_idx=set(order[:half].tolist());
train=order[:half]; test=order[half:]
print('train',len(train),'test',len(test))
print('train date range', datetime.utcfromtimestamp(TS[train[0]]), datetime.utcfromtimestamp(TS[train[-1]]))
print('test date range', datetime.utcfromtimestamp(TS[test[0]]), datetime.utcfromtimestamp(TS[test[-1]]))

import numpy as np
def stats(idxs, fn):
    r=np.array([fn(i) for i in idxs])
    return r.mean()*100, (r>0).mean()*100, len(r), np.median(r)*100

# entry subset feature extractors
def feat(i, key):
    d=ROWS[i]
    if key=='all': return 1
    snap,f=key.split('.')
    return d[snap].get(f)

print('\n=== BASELINE (exit 15s), whole full-feature universe ===')
print('train', stats(train, baseline_ret))
print('test', stats(test, baseline_ret))
print('entry slippage drift@0.8 median all:', np.median([DERIV[i][0]-1 for i in range(n)])*100,'mean',np.mean([DERIV[i][0]-1 for i in range(n)])*100)
