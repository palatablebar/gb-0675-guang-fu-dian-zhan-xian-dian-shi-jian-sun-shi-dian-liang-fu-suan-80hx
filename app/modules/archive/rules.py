import hashlib,json

def archive_decision(state):
    b=state['balance'];review=state.get('storage_summary',{}).get('review_segments',0) if state.get('storage_segments') else 0
    ready=b['row_balanced'] and b['time_balanced'] and b['energy_balanced'] and not b['unknown_segments'] and not review and not state['quarantine']
    seal_material={'energy_rows':state['energy_rows'],'storage_segments':state.get('storage_segments',[])}
    return {'status':'可归档' if ready else '待复核','seal':hashlib.sha256(json.dumps(seal_material,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
            'segments':len(state['energy_rows']),'unknown':b['unknown_segments'],'review_segments':review}
def apply(state):
    state['archive']=archive_decision(state);state['views']['archive']={'title':'运行凭证','rows':state['event_unions'],'metrics':{'切片':state['archive']['segments'],'未定':state['archive']['unknown'],'储能待查':state['archive']['review_segments'],'状态':state['archive']['status']}}
