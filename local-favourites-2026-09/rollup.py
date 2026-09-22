import json, statistics, collections, re
from urllib.parse import urlparse
D='/private/tmp/claude-501/-Users-hi/4602e2c5-c3ed-4900-8399-93f7b6b2edd2/scratchpad/local'
S=json.load(open(f'{D}/summary.json'))
rows=[json.loads(l) for l in open(f'{D}/answers.jsonl') if l.strip()]
rows=[r for r in rows if not r.get('failed')]
DIRS=("yelp","angi","thumbtack","bbb.org","nextdoor","reddit","homeadvisor","tripadvisor","facebook","healthgrades","zocdoc","yellowpages","expertise.com","threebestrated","checkatrade","trustpilot","houzz","opentable","porch","google","bing","forbes","virtuance","homeguide","birdeye","mapquest","foursquare","yell.com","thomsonlocal","truelocal","goldenpages")
def isdir(d): return any(k in d for k in DIRS)
R={}
for surface in ('chatgpt','gemini'):
    for trade in ('plumber','dentist','bakery','all'):
        cells=[v for k,v in S.items() if k.startswith(surface+'/') and (trade=='all' or k.split('/')[1].startswith(trade+'-'))]
        if not cells: continue
        top_shares=[c['top_share'] for c in cells]
        R[f'{surface}/{trade}']={
          'cells':len(cells),'n':sum(c['n'] for c in cells),
          'median_distinct':statistics.median([c['distinct'] for c in cells]),
          'median_per_answer':statistics.median([c['per_median'] for c in cells]),
          'median_top_share':round(100*statistics.median(top_shares)),
          'cells_top_ge90':sum(1 for c in cells if c['top_share']>=0.9),
          'cells_top_ge75':sum(1 for c in cells if c['top_share']>=0.75),
          'cells_top_lt50':sum(1 for c in cells if c['top_share']<0.5),
          'names_ge50_median':statistics.median([c['ge50'] for c in cells]),
          'median_first_share':round(100*statistics.median([c['first_share'] for c in cells])),
          'mean_pair':round(100*statistics.mean([c['pair'] for c in cells if c['pair'] is not None])),
          'no_name_answers':sum(c['none'] for c in cells),
          'grounded':sum(c['grounded'] for c in cells),
        }
# sources: per surface, share of grounded answers citing a directory-type domain vs a business's own site; top domains
for surface in ('chatgpt','gemini'):
    G=[r for r in rows if r['surface']==surface and r.get('grounded')]
    doms=[sorted(set(urlparse(u).netloc.replace('www.','').lower() for u in r.get('citations',[]))) for r in G]
    dcount=collections.Counter(d for x in doms for d in x)
    with_dir=sum(1 for x in doms if any(isdir(d) for d in x)); with_own=sum(1 for x in doms if any(not isdir(d) for d in x)); only_own=sum(1 for x in doms if x and all(not isdir(d) for d in x))
    R[f'{surface}/sources']={'grounded':len(G),'distinct_domains':len(dcount),'median_per_answer':statistics.median([len(x) for x in doms]) if doms else 0,
       'pct_with_directory':round(100*with_dir/len(G)) if G else None,'pct_with_own_site':round(100*with_own/len(G)) if G else None,'pct_only_own_sites':round(100*only_own/len(G)) if G else None,
       'top_directories':[(d,c) for d,c in dcount.most_common(60) if isdir(d)][:12],'top_any':dcount.most_common(12)}
    # utm markers in chatgpt links
    if surface=='chatgpt':
        urls=[u for r in G for u in r.get('citations',[])]
        R['chatgpt/links']={'total':len(urls),'with_gmb_utm':sum(1 for u in urls if 'gmb' in u.lower() or 'gbp' in u.lower()),'with_openai_utm':sum(1 for u in urls if 'utm_source=openai' in u)}
# per-city table for the post (chatgpt + gemini top share & distinct, plumber)
tbl=[]
for k,v in S.items():
    surface,q=k.split('/'); trade,city=q.split('-',1)
    tbl.append({'surface':surface,'trade':trade,'city':city,'top':v['top'][0],'top_share':round(100*v['top_share']),'distinct':v['distinct'],'first_share':round(100*v['first_share']),'pair':round(100*(v['pair'] or 0))})
R['cells']=tbl
json.dump(R, open(f'{D}/rollup.json','w'), indent=1)
for k,v in R.items():
    if k!='cells': print(k, json.dumps(v)[:400])
