"""アラートの条件が期間をまたいで安定しているかを確かめる（2026-10-11/12、alert_grade.py の数字の出所）。
alert_rank_research.py で作った場面（rank_events.pkl）を読み、業種の勢い・60日騰落・窓開け・値幅・
52週高値までの距離・引けの位置を、2022-23 / 2024 / 2025 / 2026 の4期間に分けて比べる。
入力は手元のファイル（stock-volume-screener の history_5y.pkl と data_j.xlsx）。TMPDIR に rank_events.pkl が要る。
"""
import pickle, os, sys, statistics as st, datetime as dt, collections
sys.path.insert(0, ".")
import openpyxl
T=os.environ["TMPDIR"]
evs=pickle.load(open(T+"/rank_events.pkl","rb"))
data=pickle.load(open("/Users/user/stock-volume-screener/backtest/history_5y.pkl","rb"))["data"]
# 業種（JPX一覧）
wb=openpyxl.load_workbook("/Users/user/stock-volume-screener/screener/cache/data_j.xlsx", read_only=True); ws=wb.active
sector={}
for i,row in enumerate(ws.values):
    if i==0: continue
    sector[str(row[1]).strip()]=str(row[5]).strip()
# 各銘柄の日付→位置
idx={c:{d:i for i,d in enumerate(r["dates"])} for c,r in data.items()}
# 業種ごと・日ごとの20日騰落の平均
ret20=collections.defaultdict(dict)
for c,r in data.items():
    cl=r["closes"]
    for i in range(20,len(cl)):
        if cl[i] and cl[i-20]:
            ret20[r["dates"][i]].setdefault(sector.get(c,"?"),[]).append(cl[i]/cl[i-20]-1)
secmom={d:{s:st.fmean(v) for s,v in m.items() if len(v)>=5} for d,m in ret20.items()}
mkt={d:st.median([x for v in m.values() for x in v]) for d,m in ret20.items()}
def add(e):
    r=data[e["code"]]; i=idx[e["code"]][e["date"]]; cl=r["closes"]; op=r["ohlc"]["open"]; hi=r["ohlc"]["high"]; lo=r["ohlc"]["low"]
    s=sector.get(e["code"],"?"); sm=secmom.get(e["date"],{}).get(s); m=mkt.get(e["date"])
    e["sector_rel"]=None if sm is None or m is None else sm-m
    e["mom60"]=(cl[i]/cl[i-60]-1)*100 if cl[i-60] else None
    e["gap"]=(op[i]/cl[i-1]-1)*100 if cl[i-1] and op[i] else None
    e["range"]=(hi[i]/lo[i]-1)*100 if lo[i] else None
    e["dist52"]=(cl[i]/max(hi[i-252:i])-1)*100
for e in evs: add(e)
def b(v,edges,labels):
    if v is None: return "不明"
    for x,l in zip(edges,labels):
        if v<x: return l
    return labels[-1]
F={"業種の勢い":lambda e:b(e["sector_rel"],[-0.02,0.0,0.02],["弱い","やや弱","やや強","強い"]),
   "60日騰落":lambda e:b(e["mom60"],[0,10,25],["<0%","0-10%","10-25%",">=25%"]),
   "窓開け":lambda e:b(e["gap"],[-0.5,0.5,2],["下に開け","ほぼ同値","上に0.5-2%","上に2%超"]),
   "値幅":lambda e:b(e["range"],[2,4,7],["<2%","2-4%","4-7%",">=7%"]),
   "52週高値まで":lambda e:b(e["dist52"],[-15,-5,0],["-15%より下","-15〜-5%","-5〜0%","上"]),
   "引けの位置":lambda e:b(e["close_pos"],[0.3,0.7],["弱い","中","強い"])}
P=[("22-23",dt.date(2022,1,1),dt.date(2024,1,1)),("2024",dt.date(2024,1,1),dt.date(2025,1,1)),("2025",dt.date(2025,1,1),dt.date(2026,1,1)),("2026",dt.date(2026,1,1),dt.date(2027,1,1))]
def pf(rs):
    w=sum(x for x in rs if x>0); l=-sum(x for x in rs if x<=0); return w/l if l else float("nan")
base={p:pf([e["ret"] for e in evs if a<=e["date"]<bb]) for p,a,bb in P}
for name,f in F.items():
    print(f"[{name}]")
    for k in sorted(set(f(e) for e in evs)):
        row=[];vals=[]
        for p,a,bb in P:
            rs=[e["ret"] for e in evs if a<=e["date"]<bb and f(e)==k]
            v=pf(rs)/base[p] if len(rs)>=150 else None; vals.append(v)
            row.append(f"{len(rs):5d} {v:4.2f}" if v else f"{len(rs):5d}  -  ")
        tag=" ← 4期間とも上" if all(v and v>1.05 for v in vals) else (" ← 4期間とも下" if all(v and v<0.95 for v in vals) else "")
        print(f"  {k:10s}", " | ".join(row), tag)

print("\n[注意の数（業種の勢いが弱い／52週高値まで-15%より下）]")
def caution(e):
    n=0
    if e["sector_rel"] is not None and e["sector_rel"]<-0.02: n+=1
    if e["dist52"]<-15: n+=1
    return n
for k in (0,1,2):
    row=[]
    for p,a,bb in P:
        rs=[e["ret"] for e in evs if a<=e["date"]<bb and caution(e)==k]
        w=sum(1 for x in rs if x>0)/len(rs) if rs else 0
        row.append(f"{len(rs):5d} 損益比{pf(rs):4.2f} 勝率{w*100:4.1f}%")
    print(f"  注意{k}:", " | ".join(row))
print("  全体 :", " | ".join(f"損益比{base[p]:4.2f}" for p,_,_ in P))

print("\n[点数→等級（期間ごとの損益比・件数）]")
def score(e):
    s=0
    if -5<=e["dist52"]<0: s+=1
    if e["close_pos"]<0.3: s+=1
    if e["close_pos"]>=0.7: s-=1
    if e["sector_rel"] is not None and e["sector_rel"]<-0.02: s-=1
    if e["dist52"]<-15: s-=1
    return s
def grade(s): return "SS" if s>=2 else "S" if s==1 else "A" if s==0 else "B" if s==-1 else "C"
for g in ("SS","S","A","B","C"):
    row=[]
    for p,a,bb in P:
        rs=[e["ret"] for e in evs if a<=e["date"]<bb and grade(score(e))==g]
        w=sum(1 for x in rs if x>0)/len(rs) if rs else 0
        row.append(f"{len(rs):5d} {pf(rs):4.2f} {w*100:4.1f}%")
    print(f"  {g:2s}:", " | ".join(row))
