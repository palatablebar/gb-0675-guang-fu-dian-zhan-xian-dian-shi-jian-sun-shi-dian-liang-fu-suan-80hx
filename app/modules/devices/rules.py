from app.coerce import number,instant
from app.errors import BusinessError
from app.reasons import reject
from collections import defaultdict

PV, BESS, METER = 'pv', 'bess', 'meter'
# 充电为负、放电为正；上网为正。放电不超过额定1.2倍，充电不超过-1.2倍。
def normalize(row,context):
    if row['device'] not in context['devices']:raise BusinessError('UNKNOWN_DEVICE','设备不存在')
    device=context['devices'][row['device']]
    kind=device.get('kind',PV)
    if row['kind']=='EVENT':
        start,end=instant(row['start']),instant(row['end'])
        if not start<end or (end-start).total_seconds()>86400:raise BusinessError('INVALID_RANGE','限电区间无效或超过24小时')
        return {**row,'start':start.isoformat(),'end':end.isoformat(),'zone':device['zone'],'device_kind':kind}
    if row['kind']!='TELEMETRY':raise BusinessError('BAD_KIND','记录类型无效')
    factor={'kW':1,'MW':1000}.get(row['unit'])
    if factor is None:raise BusinessError('BAD_UNIT','功率单位无效')
    power=number(row['power'])*factor
    capacity=number(device['capacity_kw'])
    if power>capacity*number('1.2'):raise BusinessError('INVALID_RANGE','功率超出范围')
    if kind in {PV,METER} and power<0:raise BusinessError('INVALID_RANGE','光伏与表计功率不可为负')
    if kind==BESS and power<-capacity*number('1.2'):raise BusinessError('INVALID_RANGE','充电功率超出范围')
    if kind==PV:
        irr=number(row['irradiance'])
        if irr<0:raise BusinessError('INVALID_RANGE','辐照度超出范围')
        if row['status'] not in {'健康','故障'}:raise BusinessError('BAD_STATUS','设备状态无效')
        return {**row,'at':instant(row['at']).isoformat(),'power_kw':str(power),'irradiance':str(irr),'zone':device['zone'],'device_kind':kind}
    # 储能与表计没有辐照通道，空列与哨兵一律视为缺测而非错误
    if row['status'] not in {'健康','故障',''}:raise BusinessError('BAD_STATUS','设备状态无效')
    return {**row,'at':instant(row['at']).isoformat(),'power_kw':str(power),'irradiance':'','zone':device['zone'],
            'status':row['status'] or '健康','device_kind':kind}
def apply(state):
    good=[]
    for row in state['records']:
        try:good.append(normalize(row,state['context']))
        except BusinessError as e:reject(state,row,e.code,e.message)
    grouped=defaultdict(list)
    for row in good:
        if row['kind']=='TELEMETRY':grouped[(row['device'],row['at'])].append(row)
    bad_ids=set()
    for rows in grouped.values():
        if len(rows)>1:
            identities={(r['power_kw'],r['irradiance'],r['status']) for r in rows}
            victims=rows if len(identities)>1 else sorted(rows,key=lambda r:r['id'])[1:]
            for row in victims:reject(state,row,'SAMPLE_CONFLICT' if len(identities)>1 else 'SAMPLE_DUPLICATE','设备同一时刻存在重复观测');bad_ids.add(row['id'])
    state['records']=[r for r in good if r['id'] not in bad_ids]
    state['views']['devices']={'title':'设备与单位','rows':state['records'],'metrics':{'设备':len(state['context']['devices']),'规范记录':len(state['records']),'隔离':len(state['quarantine'])}}
