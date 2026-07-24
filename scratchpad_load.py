import json
def price_at(traj, t):
    p=1.0
    for tt,mm in traj:
        if tt<=t: p=mm
        else: break
    return p
rows=[]
for line in open('trades/dataset.jsonl'):
    line=line.strip()
    if not line: continue
    d=json.loads(line)
    if d.get('early_traj') and d.get('outcome') and d.get('snap_15'):
        rows.append(d)
rows.sort(key=lambda r:r['created_ts'])
