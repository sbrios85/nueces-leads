import csv,re
from collections import Counter,defaultdict
SUF={'STREET','ST','DRIVE','DR','AVENUE','AVE','AV','BOULEVARD','BLVD','ROAD','RD','LANE','LN','COURT','CT','CIRCLE','CIR','TRAIL','TRL','PLACE','PL','PARKWAY','PKWY','TERRACE','TER','HIGHWAY','HWY','SQUARE','SQ','COVE','CV','POINT','PT','PLAZA','PLZ','EXPRESSWAY','EXPY','PASS','PATH','RUN','WAY','BEND','ROW','LOOP'}
DIRSET={'N','S','E','W','NE','NW','SE','SW'}
DIR={'NORTH':'N','SOUTH':'S','EAST':'E','WEST':'W','NORTHEAST':'NE','NORTHWEST':'NW','SOUTHEAST':'SE','SOUTHWEST':'SW'}
ONES={'FIRST':1,'SECOND':2,'THIRD':3,'FOURTH':4,'FIFTH':5,'SIXTH':6,'SEVENTH':7,'EIGHTH':8,'NINTH':9,'TENTH':10,'ELEVENTH':11,'TWELFTH':12,'THIRTEENTH':13,'FOURTEENTH':14,'FIFTEENTH':15,'SIXTEENTH':16,'SEVENTEENTH':17,'EIGHTEENTH':18,'NINETEENTH':19}
TENS={'TWENTIETH':20,'THIRTIETH':30,'FORTIETH':40,'FIFTIETH':50,'SIXTIETH':60,'SEVENTIETH':70,'EIGHTIETH':80,'NINETIETH':90}
TENS_P={'TWENTY':20,'THIRTY':30,'FORTY':40,'FIFTY':50,'SIXTY':60,'SEVENTY':70,'EIGHTY':80,'NINETY':90}
ONES_P={'FIRST':1,'SECOND':2,'THIRD':3,'FOURTH':4,'FIFTH':5,'SIXTH':6,'SEVENTH':7,'EIGHTH':8,'NINTH':9}
JUNK={'WTR','STE','APT','UNIT','BLDG','FL','RM','SUITE','LOT','TRLR','SP','SPC','GENER','GEN'}
def ordsuf(n): return f"{n}{'TH' if 10<=n%100<=20 else {1:'ST',2:'ND',3:'RD'}.get(n%10,'TH')}"
def ow2n(toks):
    out=[];i=0
    while i<len(toks):
        t=toks[i]
        if t in ONES: out.append(ordsuf(ONES[t]));i+=1;continue
        if t in TENS: out.append(ordsuf(TENS[t]));i+=1;continue
        if t in TENS_P and i+1<len(toks) and toks[i+1] in ONES_P: out.append(ordsuf(TENS_P[t]+ONES_P[toks[i+1]]));i+=2;continue
        out.append(t);i+=1
    return out
def _toks(s):
    s=str(s).upper()
    s=s.replace("'","")                    # O'NEILL -> ONEILL
    s=re.sub(r'\bBL\d+\b',' ',s)          # block codes BL002 etc
    s=re.sub(r'[.,#&/]',' ',s)
    s=re.sub(r'\s+',' ',s).strip()
    toks=[DIR.get(t,t) for t in s.split()]
    toks=ow2n(toks)
    # join MC + next  (MC DONALD -> MCDONALD)
    j=[];i=0
    while i<len(toks):
        if toks[i]=='MC' and i+1<len(toks): j.append('MC'+toks[i+1]);i+=2;continue
        j.append(toks[i]);i+=1
    toks=j
    # cut everything from a junk/service token onward
    for idx,t in enumerate(toks):
        if t in JUNK: toks=toks[:idx]; break
    # strip trailing unit token after a suffix (ST 1, LN C, DR 101)
    while len(toks)>=2 and toks[-2] in SUF and (len(toks[-1])<=2 or toks[-1].isdigit()) and toks[-1] not in DIRSET: toks=toks[:-1]
    # drop suffix words
    toks=[t for t in toks if t not in SUF]
    return toks
def key_full(toks): return ' '.join(toks)
def key_noprefix(toks):
    # drop a leading directional that sits right after the house number
    if len(toks)>=3 and toks[1] in DIRSET: return toks[0]+' '+' '.join(toks[2:])
    return ' '.join(toks)
def build_index(path):
    rows=list(csv.DictReader(open(path,encoding="utf-8-sig"),delimiter="|"))
    full=defaultdict(list); nop=defaultdict(list)
    for r in rows:
        if not r["situs_num"].strip(): continue
        cls=(r["imprv_state_cd"].strip() or r["land_state_cd"].strip())
        t=_toks(f"{r['situs_num']} {r['situs_prefx']} {r['situs_street']} {r['situs_suffix']}")
        if not t: continue
        full[key_full(t)].append((r["prop_id"].strip(),cls))
        nop[key_noprefix(t)].append((r["prop_id"].strip(),cls))
    return full,nop
def match(addr,full,nop):
    t=_toks(addr)
    k=key_full(t)
    if k in full: return ('full',full[k])
    kn=key_noprefix(t)
    if kn in nop:
        cls=set(c for _,c in nop[kn] if c)
        if len(cls)<=1: return ('noprefix',nop[kn])   # accept only if unambiguous class
    return (None,None)
