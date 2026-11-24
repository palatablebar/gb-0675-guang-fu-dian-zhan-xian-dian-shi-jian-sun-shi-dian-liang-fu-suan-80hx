from decimal import Decimal
from app.coerce import number, instant
from app.modules.devices.rules import PV, BESS, METER

# 认定原因码：
# GRID_CURTAIL  并网约束限发，可申报
# PV_OWN_LOSS   光伏自身减发（无并网约束），不可申报
# NO_LOSS       无限发
# DATA_GAP      三量缺测，留待查
# BALANCE_FAIL  光伏-电池-上网账差超阈，留待查
# OVER_DELIVER  实测上网高于自然净出口，留待查
REVIEWABLE = {'DATA_GAP', 'BALANCE_FAIL', 'OVER_DELIVER'}


def _device_kind(context, device):
    return context['devices'].get(device, {}).get('kind', PV)


def _power_index(records, context, kind):
    """设备 -> 采样时刻(UTC iso) -> 最新功率字符串。光伏只取发电主体，对照逆变器不进并网点三量。"""
    index = {}
    for row in records:
        if row['kind'] != 'TELEMETRY' or _device_kind(context, row['device']) != kind:
            continue
        if kind == PV and context['devices'][row['device']].get('reference'):
            continue
        index.setdefault(row['device'], {})[row['at']] = row['power_kw']
    return index


def _sum_at(index, at):
    values = [series.get(at) for series in index.values() if at in series]
    if not values:
        return None
    return sum((number(v) for v in values), Decimal(0))


def _slot(iso, pv_index, bess_index, meter_index, seconds, context):
    pv_kw = _sum_at(pv_index, iso)
    bess_kw = _sum_at(bess_index, iso)
    export_kw = _sum_at(meter_index, iso)
    battery_source = 'telemetry'
    # 上游充放依据只修正已有电池遥测的槽位；缺测槽位不臆造，保持待查
    if bess_kw is not None:
        if context.get('charge_kw') is not None:
            bess_kw = -number(context['charge_kw'])
            battery_source = 'basis'
        elif context.get('discharge_kw') is not None:
            bess_kw = number(context['discharge_kw'])
            battery_source = 'basis'
    charge_kw = max(Decimal(0), -bess_kw) if bess_kw is not None else None
    discharge_kw = max(Decimal(0), bess_kw) if bess_kw is not None else None
    return {'start': iso, 'end': '', 'seconds': seconds,
            'pv_kw': pv_kw, 'bess_kw': bess_kw, 'export_kw': export_kw,
            'charge_kw': charge_kw, 'discharge_kw': discharge_kw,
            'battery_source': battery_source if bess_kw is not None else 'gap'}


def build_segments(records, context):
    """按采样节奏把全场划成半开区间，逐段并列光伏、电池、上网三个功率；
    节奏内缺测保持待查，超出节奏的缺口另立缺测片段，绝不外推。"""
    from datetime import timedelta
    pv_index, bess_index, meter_index = (_power_index(records, context, k) for k in (PV, BESS, METER))
    cadence = int(context['cadence_seconds'])
    stamps = set()
    for index in (pv_index, bess_index, meter_index):
        for series in index.values():
            stamps.update(series)
    stamps = sorted(instant(t) for t in stamps)
    rows = []
    for i, at in enumerate(stamps):
        iso = at.isoformat()
        if i + 1 < len(stamps):
            gap = int((stamps[i + 1] - at).total_seconds())
            if gap <= cadence:
                row = _slot(iso, pv_index, bess_index, meter_index, gap, context)
                row['end'] = stamps[i + 1].isoformat()
            else:
                row = _slot(iso, pv_index, bess_index, meter_index, cadence, context)
                row['end'] = (at + timedelta(seconds=cadence)).isoformat()
                blank = _slot((at + timedelta(seconds=cadence)).isoformat(),
                              pv_index, bess_index, meter_index, gap - cadence, context)
                blank['end'] = stamps[i + 1].isoformat()
                rows.extend((row, blank))
                continue
        else:
            row = _slot(iso, pv_index, bess_index, meter_index, cadence, context)
            row['end'] = (at + timedelta(seconds=cadence)).isoformat()
        rows.append(row)
    return rows


def adjudicate(segment, context):
    """每段都由光伏发电、电池充放、上网功率互证；电池充放量永不计入限发。"""
    hours = Decimal(segment['seconds']) / 3600
    pv_kw, export_kw = segment['pv_kw'], segment['export_kw']
    charge_kw, discharge_kw = segment['charge_kw'], segment['discharge_kw']
    limit = number(context.get('export_limit_kw'))
    tolerance = number(context.get('storage_tolerance_kwh'))
    out = {k: segment[k] for k in ('start', 'end', 'seconds')}
    out['battery_source'] = segment.get('battery_source', 'telemetry')
    for key, val in (('pv_kw', pv_kw), ('export_kw', export_kw),
                     ('charge_kw', charge_kw), ('discharge_kw', discharge_kw)):
        out[key] = None if val is None else str(val)
    out['export_limit_kw'] = str(limit)
    if pv_kw is None or export_kw is None or charge_kw is None:
        out.update(_energy(None, None, None, None, None, hours))
        out.update(reason_code='DATA_GAP', decision='待查', review_flag=True, basis_revision=context['basis_revision'])
        return out
    pv_kwh = pv_kw * hours
    charge_kwh = charge_kw * hours
    discharge_kwh = discharge_kw * hours
    export_kwh = export_kw * hours
    # 自然净出口：不经过电池充放时本应上网的电量
    natural_kwh = pv_kwh - charge_kwh + discharge_kwh
    residual = natural_kwh - export_kwh
    limit_kwh = limit * hours
    # 净差法（按净出口差额）会把充电当限电、被放电冲掉损失 —— 仅作对照展示
    naive_claim = max(Decimal(0), pv_kwh - export_kwh)
    # 并网约束是否生效：自然净出口电量超过限值电量（容差只吸收计量尾差，不吸收整段缺口）
    binding = natural_kwh > limit_kwh + tolerance
    grid_curtail = max(Decimal(0), natural_kwh - limit_kwh)
    if binding:
        # 限发时段实测上网应落在限值计量带内；越带说明三量无法互证，留待查
        if export_kwh > limit_kwh + tolerance:
            claim = Decimal(0)
            code = 'OVER_DELIVER'
        elif export_kwh < limit_kwh - tolerance and abs(residual - grid_curtail) > tolerance:
            claim = Decimal(0)
            code = 'BALANCE_FAIL'
        else:
            # 约束缺口即限发量；电池充放已在自然净出口中剔除，不计入限发
            claim = grid_curtail
            code = 'GRID_CURTAIL'
    else:
        claim = Decimal(0)
        if residual < -tolerance:
            code = 'OVER_DELIVER'
        elif abs(residual) > tolerance:
            code = 'BALANCE_FAIL'
        else:
            # 约束未生效：任何减发都是光伏自身原因，电池动作不构成限发
            code = 'PV_OWN_LOSS' if residual > tolerance else 'NO_LOSS'
    battery_impact = naive_claim - claim
    out.update(_energy(pv_kwh, charge_kwh, discharge_kwh, export_kwh, natural_kwh, hours,
                       residual, grid_curtail, claim, naive_claim, battery_impact))
    out.update(reason_code=code, decision=('可申报' if code == 'GRID_CURTAIL' else
               ('待查' if code in REVIEWABLE else '不可申报')),
               review_flag=code in REVIEWABLE, basis_revision=context['basis_revision'])
    return out


def _energy(pv_kwh, charge_kwh, discharge_kwh, export_kwh, natural_kwh, hours,
            residual=None, grid_curtail=None, claim=None, naive=None, impact=None):
    def s(v):
        return None if v is None else str(v)
    return {'pv_kwh': s(pv_kwh), 'charge_kwh': s(charge_kwh), 'discharge_kwh': s(discharge_kwh),
            'export_kwh': s(export_kwh), 'natural_kwh': s(natural_kwh),
            'residual_kwh': s(residual), 'grid_curtail_kwh': s(grid_curtail),
            'claimable_kwh': s(claim), 'naive_claim_kwh': s(naive), 'battery_impact_kwh': s(impact)}


def summarize(rows):
    def total(key, review_only=False):
        acc = Decimal(0)
        for r in rows:
            if r[key] is None or (review_only and not r['review_flag']):
                continue
            acc += number(r[key])
        return str(acc.quantize(Decimal('.0001')))
    review = [r for r in rows if r['review_flag']]
    return {'segments': len(rows),
            'claimable_kwh': total('claimable_kwh'),
            'naive_claim_kwh': total('naive_claim_kwh'),
            'battery_impact_kwh': total('battery_impact_kwh'),
            'charge_kwh': total('charge_kwh'),
            'discharge_kwh': total('discharge_kwh'),
            'review_segments': len(review),
            'review_seconds': sum(r['seconds'] for r in review)}


def apply(state):
    from app.config import EXPORT_LIMIT_KW, STORAGE_TOLERANCE_KWH
    context = state['context']
    context.setdefault('export_limit_kw', EXPORT_LIMIT_KW)
    context.setdefault('storage_tolerance_kwh', STORAGE_TOLERANCE_KWH)
    # 首跑规则结果记为 r0；上游依据账本从 r1 开始，重算时显式覆盖
    context['basis_revision'] = context.get('basis_revision', 0)
    kinds = {d.get('kind', PV) for d in context['devices'].values()}
    if BESS not in kinds or METER not in kinds:
        # 纯光伏场没有共用并网点表计，本模块不产生片段也不阻断归档
        state['storage_segments'] = []
        state['storage_summary'] = {'segments': 0, 'claimable_kwh': '0.0000', 'naive_claim_kwh': '0.0000',
                                    'battery_impact_kwh': '0.0000', 'charge_kwh': '0.0000', 'discharge_kwh': '0.0000',
                                    'review_segments': 0, 'review_seconds': 0}
        state['views']['storage'] = {'title': '储能并网点仲裁（本场无储能/表计）', 'rows': [],
                                      'metrics': {'可申报损失_kWh': '0.0000', '电池影响_kWh': '0.0000', '待查片段': 0}}
        return
    raw_segments = build_segments(state['records'], context)
    rows = [adjudicate(s, context) for s in raw_segments]
    state['storage_segments'] = rows
    state['storage_summary'] = summarize(rows)
    state['views']['storage'] = {'title': '储能并网点仲裁', 'rows': rows,
                                  'metrics': {'可申报损失_kWh': state['storage_summary']['claimable_kwh'],
                                              '电池影响_kWh': state['storage_summary']['battery_impact_kwh'],
                                              '待查片段': state['storage_summary']['review_segments']}}
