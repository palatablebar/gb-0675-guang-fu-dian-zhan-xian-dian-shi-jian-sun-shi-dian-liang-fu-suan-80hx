import sqlite3, json, hashlib
from decimal import Decimal
from app.errors import BusinessError
from app.config import BASIS_SCOPE
from app.coerce import number


def _connect(path):
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("""CREATE TABLE IF NOT EXISTS basis_ledger(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        basis_json TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(run_id, revision))""")
    return con


def _validate(basis, devices=None):
    if not isinstance(basis, dict):
        raise BusinessError('BAD_BASIS', '依据须为对象')
    unknown = set(basis) - BASIS_SCOPE
    if unknown:
        raise BusinessError('BAD_BASIS', f'未知依据字段: {sorted(unknown)}')
    if 'charge_kw' in basis and 'discharge_kw' in basis:
        raise BusinessError('BAD_BASIS', '同一依据不可同时给出充电与放电功率')
    clean = {}
    for key in BASIS_SCOPE:
        if key not in basis or basis[key] is None:
            continue
        try:
            val = number(basis[key])
        except BusinessError:
            raise BusinessError('BAD_BASIS', f'{key} 数值无效')
        if val < 0:
            raise BusinessError('BAD_BASIS', f'{key} 不可为负')
        clean[key] = str(val)
    if not clean:
        raise BusinessError('BAD_BASIS', '依据为空')
    if devices is not None and ({'charge_kw', 'discharge_kw'} & set(clean)):
        from app.coerce import number as _num
        capacity = sum((_num(d['capacity_kw']) for d in devices.values()
                        if d.get('kind') == 'bess'), Decimal(0))
        for key in ('charge_kw', 'discharge_kw'):
            if key in clean and _num(clean[key]) > capacity * Decimal('1.2'):
                raise BusinessError('BAD_BASIS', f'{key} 超过电池额定 1.2 倍')
    return clean


def _hash(basis):
    return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def list_basis(path, run_id):
    with _connect(path) as con:
        rows = con.execute('SELECT revision,basis_json,sha256,note,created_at FROM basis_ledger '
                           'WHERE run_id=? ORDER BY revision', (run_id,)).fetchall()
    return [{'revision': r['revision'], 'basis': json.loads(r['basis_json']),
             'sha256': r['sha256'], 'note': r['note'], 'created_at': r['created_at']} for r in rows]


def latest_basis(path, run_id):
    entries = list_basis(path, run_id)
    return entries[-1] if entries else None


def submit_basis(path, run_id, basis, note=''):
    """上游充放依据更新：同一依据重试不新增版本；校验失败保持旧版；只重算受影响片段。"""
    precheck = _validate(basis)
    con = _connect(path)
    try:
        # 先在事务内取批与下一版本号，任何失败都回滚，旧版依据与现场凭据原样保留
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT result FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise BusinessError('NOT_FOUND', '批次不存在', 404)
        result = json.loads(row['result'])
        if not result.get('storage_segments'):
            raise BusinessError('NO_STORAGE', '该批次没有储能并网点片段，无法应用充放依据')
        clean = _validate(basis, result['context']['devices'])
        digest = _hash(clean)
        entries = list_basis(path, run_id)
        for entry in entries:
            if entry['sha256'] == digest:
                con.rollback()
                return {'run_id': run_id, 'basis_revision': entry['revision'], 'changed': False,
                        'affected_segments': 0, 'note': '同一依据重试，不新增电量'}
        # 首跑规则结果记为 r0；第一条上游依据为 r1，之后递增
        revision = (entries[-1]['revision'] + 1) if entries else 1
        affected = _affected_segments(result, clean)
        con.execute('INSERT INTO basis_ledger(run_id,revision,basis_json,sha256,note) VALUES(?,?,?,?,?)',
                    (run_id, revision, json.dumps(clean, ensure_ascii=False), digest, str(note or '')))
        _reapply(result, clean, revision, affected)
        con.execute('UPDATE runs SET result=? WHERE id=?', (json.dumps(result, ensure_ascii=False), run_id))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return {'run_id': run_id, 'basis_revision': revision, 'changed': True,
            'affected_segments': len(affected), 'note': note}


def _affected_segments(result, basis):
    """只挑依据窗口内的片段键（start/end），其余片段不重算。"""
    keys = set()
    for seg in result.get('storage_segments', []):
        hit = False
        if 'charge_kw' in basis or 'discharge_kw' in basis:
            hit = hit or seg.get('charge_kw') is not None or seg.get('discharge_kw') is not None
        if 'export_limit_kw' in basis:
            hit = True
        if hit:
            keys.add((seg['start'], seg['end']))
    return keys


def _reapply(result, basis, revision, affected):
    from app.modules.storage.rules import build_segments, adjudicate, summarize
    context = result['context']
    # 充放依据是逐版本替换而非叠加，先清掉上一版可能残留的充放键
    for key in ('charge_kw', 'discharge_kw'):
        context.pop(key, None)
    context.update(basis)
    context['basis_revision'] = revision
    fresh = {s['start']: s for s in (adjudicate(raw, context)
                                     for raw in build_segments(result['records'], context))}
    for seg in result['storage_segments']:
        if (seg['start'], seg['end']) in affected and seg['start'] in fresh:
            new = fresh[seg['start']]
            seg.clear()
            seg.update(new)
    result['storage_summary'] = summarize(result['storage_segments'])
    result['views']['storage'] = {'title': '储能并网点仲裁', 'rows': result['storage_segments'],
                                   'metrics': {'可申报损失_kWh': result['storage_summary']['claimable_kwh'],
                                               '电池影响_kWh': result['storage_summary']['battery_impact_kwh'],
                                               '待查片段': result['storage_summary']['review_segments']}}
    result.setdefault('basis_history', []).append({'revision': revision, 'basis': basis})
