"""Compact, whole-record evidence selection. Budget includes every body label, not global instructions."""
import json
import re
from app.config import get_settings
from app.memory import text

PROTOCOL='evidence-selection/1'


def broad_query(query):
    return bool(re.search(r'\b(?:list|all|both|each|common|compare|between|would|why)\b|哪些|有哪些|共同|分别|如果|比较|'
        r'\b(?:what|which)\s+(?:\w+\s+){0,2}\w+s\b|\bwhat\s+(?:do|does|are)\b|\bwhere\b.*\b(?:has|have|did)\b',query,re.I))


def metadata(hit):
    ctx=hit.fact_context or {}
    out={}
    if hit.event_time:out['event_time']=hit.event_time.isoformat()
    if ctx.get('state') and ctx['state']!='unknown':out['state']=ctx['state']
    t=ctx.get('time') or {}
    for key in ('raw','anchor','start','end','precision'):
        if t.get(key) and t[key]!='unknown':out['time_'+key]=t[key]
    # Preserve identity and record time without repeating the same instruction per source.
    sources=ctx.get('sources') or []
    out['sources']=[{k:x[k] for k in ('message_id','speaker','recorded_at') if x.get(k)} for x in sources[:6]]
    if not out['sources']:out.pop('sources')
    g=ctx.get('structured_graph') or {}
    g=g.get('graph',g)
    entities={x.get('ref'):x.get('name','') for x in g.get('entities',[])}
    body=hit.text.casefold()
    detail=[]
    for rel in g.get('relations',[]):
        a=entities.get(rel.get('subject'),'')
        b=entities.get(rel.get('object_entity'),'') or rel.get('object_text','')
        if (a and a.casefold() not in body) or (b and b.casefold() not in body):
            detail.append({'subject':a,'relation':rel.get('relation'),'object':b})
    for event in g.get('events',[]):
        for p in event.get('participants',[]):
            if p.get('name') and p['name'].casefold() not in body:detail.append(p)
        for k,v in (event.get('attributes') or {}).items():
            if str(v).casefold() not in body:detail.append({k:v})
    if detail:out['precision']=detail
    return json.dumps(out,ensure_ascii=False,separators=(',',':')) if out else ''


def render(hit,index):
    return f'[记忆 {index}] {hit.text}' + ('\n'+metadata(hit) if metadata(hit) else '')


def tokens(value):
    from app.memory.graph.ranking import _STOP
    return {x for x in text.tokenize(value) if x not in _STOP}


def select_facts(result):
    """Keep complete fact records; diversify broad questions without inventing an exhaustive answer."""
    s=get_settings()
    pool=list({x.memory_id:x for x in (result.candidates or result.hits)}.values())
    picked=[]; blocks=[]; used=0; dropped=[]
    diverse=broad_query(result.query)
    remaining=list(enumerate(pool))
    by_id={h.memory_id:h for h in pool}
    paths=result.trace.get('graph',{}).get('paths',[])
    rejected_paths=[]
    while remaining and len(picked)<(result.selection_limit or max(1,s.memory_top_k)):
        def utility(pair):
            rank,hit=pair
            overlap=max((len(tokens(hit.text)&tokens(h.text))/max(1,len(tokens(hit.text)|tokens(h.text))) for h in picked),default=0)
            return 1/(rank+2)*(1-.75*overlap if diverse else 1)
        best=max(remaining,key=utility)
        remaining.remove(best)
        hit=best[1]
        group=next((p['memory_ids'] for p in paths if len(p['memory_ids'])>1 and hit.memory_id in p['memory_ids']),[hit.memory_id])
        remaining=[pair for pair in remaining if pair[1].memory_id not in group]
        if any(mid not in by_id for mid in group):
            rejected_paths.append({'memory_ids':group,'reason':'missing_validated_member'})
            continue
        chosen={h.memory_id for h in picked}
        members=[by_id[mid] for mid in dict.fromkeys(group) if mid not in chosen]
        new_blocks=[render(h,len(picked)+i+1) for i,h in enumerate(members)]
        if (len(picked)+len(members)>(result.selection_limit or max(1,s.memory_top_k)) or
                len('\n'.join(blocks+new_blocks))>max(0,s.memory_context_chars)):
            dropped.extend(h.memory_id for h in members)
            if len(group)>1:rejected_paths.append({'memory_ids':group,'reason':'quantity_or_character_budget'})
            continue
        blocks.extend(new_blocks);picked.extend(members);used=len('\n'.join(blocks))
    result.hits=picked
    result.source_context='\n'.join(blocks)
    result.counts['final']=len(picked)
    result.trace['selection']={'protocol':PROTOCOL,'body_chars':used,'body_budget':s.memory_context_chars,
        'dropped_for_budget':dropped,'diversity_heuristic':diverse,'candidate_count':len(pool),'rejected_paths':rejected_paths}
    chosen={h.memory_id for h in picked}
    paths=result.trace.get('graph',{}).get('paths',[])
    result.trace['selection']['complete_paths']=[p['memory_ids'] for p in paths if set(p['memory_ids'])<=chosen]
    result.trace['selection']['incomplete_paths']=[p['memory_ids'] for p in paths if set(p['memory_ids'])&chosen and not set(p['memory_ids'])<=chosen]
    if dropped:
        result.degraded.append('证据超出上下文预算，已跳过无法完整容纳的记忆')
    if rejected_paths:
        result.degraded.append('图证据组合未完整满足校验或预算，未作为完整路径使用')
    return result
