import json, re, collections, statistics, math
from urllib.parse import urlparse
D='/private/tmp/claude-501/-Users-hi/4602e2c5-c3ed-4900-8399-93f7b6b2edd2/scratchpad/local'
rows=[json.loads(l) for l in open(f'{D}/answers.jsonl') if l.strip()]
rows=[r for r in rows if not r.get('failed')]
STOP=re.compile(r"^(best|why|who|what|watch|the|pros|cons|drawback|ideal|pricing|choose|go|if|downside|starting|key|bottom|final|top|quick|my|note|free|tip|summary|how|for|verdict|overall|price|cost|great|good|strong|also|other|honorable|runner|budget|simple|cheap|consider|try|look|pick|standout|weak|caveat|limit|catch|con|pro|use|get|start|step|option|category|feature|plan|tier|recommend|decision|comparison|conclusion|bonus|alternative|open now|closed|rating|reviews?|address|phone|website|hours|services?|specialt|emergency|residential|commercial|local|independent|large|national|chain|before you|things to|questions to|ask|check|verify|call|read|compare|a |an |in |on |at |to |of |and |or |but |so |as |is |are |it|you|your|we |our|they|their|here|there|this|that|these|those|important|disclaimer|remember|always|make sure|be sure|when|where|which|since|because|based|according|popular|highly|well|award|family|most|many|some|several|two|three|four|five|first|second|third|next|last|one)\b", re.I)
HEAD=re.compile(r"^\s*(?:#{1,4}\s*|\d+[.)]\s*|[-*•]\s*)?\**\[?([A-Z][^\]\n*:(|–—]{2,60}?)\]?(?:\([^)]*\))?\**\s*(?::|—|–|-\s|\(|\*\*|$)", re.M)
def norm(n):
    n=re.sub(r"\s*\((?:[^)]*)\)\s*"," ",n); n=re.sub(r"[’'`]","",n)
    n=re.sub(r"\b(llc|inc|ltd|co|corp|pllc|dds|dmd|pc|pa)\b\.?","",n,flags=re.I)
    n=re.sub(r"[^A-Za-z0-9& ]+"," ",n); n=re.sub(r"\s+"," ",n).strip().lower()
    return n
def names(answer):
    out=[]; seen=set()
    for m in HEAD.finditer(answer):
        raw=m.group(1).strip()
        if len(raw)<3 or STOP.match(raw) or raw.lower() in ("plumber","dentist","bakery"): continue
        if len(raw.split())>7: continue
        if re.match(r"^(tips?|how|questions?|things|factors|what|final|choosing|considerations?|availability|licens|pricing|response|experience|emergency|specialt|insurance|warranty|guarantee|hours|location|contact|about|overview|summary|verdict|conclusion|note|source|sources|references?|methodology|criteria|red flags?|pro tip|bonus|honou?rable|runner|also|other|more|additional|nearby|neighbou?rhood|downtown|north|south|east|west|central|reputation|nhs|private|public|cost|budget|quality|speed|trust|value|verified|reviews?|ratings?|online|word of mouth|community|customer)\b", raw, re.I): continue
        if re.search(r"\b(vs\.?|versus)\b", raw, re.I): continue
        if re.search(r"\b(for hiring|to consider|to ask|to look|to avoid|before you|when choosing|how to|checklist|comparison)\b", raw, re.I): continue
        n=norm(raw)
        if not n or n in seen: continue
        seen.add(n); out.append(n)
    return out
def merge(cellnames):
    """Within a cell, fold a name into a longer one that starts with it (radiant plumbing → radiant plumbing & air)."""
    allc=collections.Counter(n for L in cellnames for n in L)
    keys=sorted(allc, key=len)
    alias={}
    for k in keys:
        for k2 in keys:
            if k2!=k and k2.startswith(k+" ") and allc[k2]>=allc[k]: alias[k]=k2; break
    def res(n):
        while n in alias: n=alias[n]
        return n
    return [list(dict.fromkeys(res(n) for n in L)) for L in cellnames]
def wilson(k,n,z=1.96):
    if n==0: return (0,0)
    p=k/n; d=1+z*z/n; c=(p+z*z/(2*n))/d; h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return (max(0,c-h), min(1,c+h))
def jacc(a,b):
    a,b=set(a),set(b); return len(a&b)/len(a|b) if a|b else 1.0
DIRS=("yelp","google","angi","thumbtack","bbb","nextdoor","reddit","homeadvisor","tripadvisor","facebook","healthgrades","zocdoc","yellowpages","expertise","threebestrated","checkatrade","trustpilot","houzz","opentable")
out={}
for (surface,q) in sorted(set((r['surface'],r['q']) for r in rows)):
    R=[r for r in rows if r['surface']==surface and r['q']==q]; n=len(R)
    L=merge([names(r['answer']) for r in R])
    app=collections.Counter(b for x in L for b in set(x)); first=collections.Counter(x[0] for x in L if x)
    pair=[jacc(L[i],L[j]) for i in range(n) for j in range(i+1,n)]
    top=app.most_common(1)[0] if app else ('-',0)
    doms=[sorted(set(urlparse(u).netloc.replace('www.','') for u in r.get('citations',[]))) for r in R if r.get('grounded')]
    dapp=collections.Counter(d for x in doms for d in x)
    dir_share=sum(1 for x in doms if any(any(k in d for k in DIRS) for d in x))/len(doms) if doms else None
    own_site=sum(1 for x in doms if any(not any(k in d for k in DIRS) for d in x))/len(doms) if doms else None
    out[f"{surface}/{q}"]={'n':n,'grounded':sum(1 for r in R if r.get('grounded')),'distinct':len(app),'per_median':statistics.median([len(x) for x in L]) if L else 0,
        'none':sum(1 for x in L if not x),'top':top,'top_share':top[1]/n,'ge90':sum(1 for c in app.values() if c/n>=0.9),'ge50':sum(1 for c in app.values() if c/n>=0.5),
        'first':first.most_common(3),'first_share':(first.most_common(1)[0][1]/n) if first else 0,'pair':statistics.mean(pair) if pair else None,
        'rates':[(b,c,round(100*c/n),[round(100*x) for x in wilson(c,n)]) for b,c in app.most_common(12)],'domains':dapp.most_common(8),'dir_share':dir_share,'own_site_share':own_site}
json.dump(out, open(f'{D}/summary.json','w'), indent=1)
# roll-ups
for s in ('chatgpt','gemini'):
    cells=[v for k,v in out.items() if k.startswith(s+'/')]
    if not cells: continue
    print(f"=== {s}: cells={len(cells)} n/cell={cells[0]['n']} | median distinct={statistics.median([c['distinct'] for c in cells])} | median top share={statistics.median([c['top_share'] for c in cells]):.2f} | cells with any ≥90% name={sum(1 for c in cells if c['ge90']>0)} | with any ≥50%={sum(1 for c in cells if c['ge50']>0)} | mean pairJ={statistics.mean([c['pair'] for c in cells if c['pair'] is not None]):.2f} | no-name answers={sum(c['none'] for c in cells)} | first-named modal share median={statistics.median([c['first_share'] for c in cells]):.2f}")
    d=collections.Counter()
    for c in cells:
        for dom,cnt in c['domains']: d[dom]+=cnt
    print("   top domains:", d.most_common(12))
for k,v in list(out.items())[:4]:
    print(f"\n-- {k}: distinct={v['distinct']} top={v['top']} ≥50%={v['ge50']} none={v['none']}"); print("   ", v['rates'][:6])
