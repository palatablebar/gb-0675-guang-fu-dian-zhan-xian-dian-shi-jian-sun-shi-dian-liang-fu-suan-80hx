from app.engine import evaluate
from app.modules.storage.rules import adjudicate, build_segments, summarize
from app import basis, store
from app.errors import BusinessError
from decimal import Decimal
from copy import deepcopy
import pytest


@pytest.fixture
def storage():
    return evaluate({'dataset': 'storage'})


def _by_time(state):
    return {r['start'][11:16]: r for r in state['storage_segments']}


def test_pv_only_run_skips_storage(clean):
    assert clean['storage_segments'] == []
    assert clean['storage_summary']['review_segments'] == 0
    assert clean['archive']['status'] == '可归档'


def test_six_segment_decisions(storage):
    rows = _by_time(storage)
    assert rows['02:00']['reason_code'] == 'NO_LOSS'
    assert rows['02:10']['reason_code'] == 'NO_LOSS'          # 充电压低上网但非限发
    assert rows['02:20']['reason_code'] == 'GRID_CURTAIL'     # 放电补回，约束缺口可申报
    assert rows['02:30']['reason_code'] == 'BALANCE_FAIL'
    assert rows['02:40']['reason_code'] == 'BALANCE_FAIL'
    assert rows['02:50']['reason_code'] == 'DATA_GAP'


def test_charge_never_counted_as_curtailment(storage):
    from decimal import Decimal
    row = _by_time(storage)['02:10']
    # 净差法会把 40kW 充电误算成 6.6667kWh 限发
    assert Decimal(row['naive_claim_kwh']).quantize(Decimal('.0001')) == Decimal('6.6667')
    assert Decimal(row['claimable_kwh']) == 0
    assert Decimal(row['charge_kwh']).quantize(Decimal('.0001')) == Decimal('6.6667')
    assert Decimal(row['battery_impact_kwh']).quantize(Decimal('.0001')) == Decimal('6.6667')


def test_discharge_does_not_offset_real_curtailment(storage):
    from decimal import Decimal
    row = _by_time(storage)['02:20']
    # 放电 5kWh 把实测上网托回限值；净差法算出 0，真实约束缺口 5kWh 仍可申报
    assert Decimal(row['discharge_kwh']).quantize(Decimal('.0001')) == Decimal('5.0000')
    assert Decimal(row['naive_claim_kwh']) == 0
    assert Decimal(row['claimable_kwh']) == Decimal('5.0000')
    assert row['decision'] == '可申报'


def test_three_way_energy_explained_per_segment(storage):
    for row in storage['storage_segments']:
        if row['reason_code'] == 'DATA_GAP':
            assert row['pv_kwh'] is None or row['export_kwh'] is None
            assert row['review_flag']
            continue
        # 光伏发电 + 放电 - 充电 = 上网电量 + 账差，三量互证
        pv = Decimal(row['pv_kwh']) + Decimal(row['discharge_kwh']) - Decimal(row['charge_kwh'])
        assert pv == Decimal(row['export_kwh']) + Decimal(row['residual_kwh'])


def test_review_segments_excluded_from_claim(storage):
    summary = storage['storage_summary']
    assert summary['claimable_kwh'] == '5.0000'
    assert summary['review_segments'] == 3
    assert summary['review_seconds'] == 1800
    # 净差法误算 16.6667，其中电池影响 11.6667，可申报只有 5
    assert summary['naive_claim_kwh'] == '16.6667'
    assert summary['battery_impact_kwh'] == '11.6667'


def test_data_gap_segment(storage):
    row = _by_time(storage)['02:50']
    assert row['export_kw'] is None and row['review_flag']
    assert row['claimable_kwh'] is None


def test_balance_failure_is_review_not_zero(storage):
    for t in ('02:30', '02:40'):
        row = _by_time(storage)[t]
        assert row['review_flag'] and row['decision'] == '待查'
        assert row['claimable_kwh'] == '0'


def test_basis_retry_is_idempotent(tmp_path):
    db = str(tmp_path / 'b.sqlite')
    run = store.save(db, evaluate({'dataset': 'storage'}))
    payload = {'export_limit_kw': '240'}
    first = basis.submit_basis(db, run['manifest']['run_id'], payload, note='单据A')
    second = basis.submit_basis(db, run['manifest']['run_id'], dict(payload), note='单据A重复')
    assert first['changed'] and first['basis_revision'] == 1
    assert not second['changed'] and second['basis_revision'] == 1
    assert len(basis.list_basis(db, run['manifest']['run_id'])) == 1


def test_basis_update_only_recomputes_affected(tmp_path):
    db = str(tmp_path / 'c.sqlite')
    saved = store.save(db, evaluate({'dataset': 'storage'}))
    rid = saved['manifest']['run_id']
    before = {r['start']: r['claimable_kwh'] for r in saved['storage_segments']}
    rows_before = _by_time(saved)
    assert rows_before['02:20']['reason_code'] == 'GRID_CURTAIL'
    # 上游依据 r1：限值放宽到 290，约束解除，原本被约束解释的账差暴露为待查
    out = basis.submit_basis(db, rid, {'export_limit_kw': '290'}, note='限值放宽至290')
    updated = store.get(db, rid)
    assert out['changed'] and out['affected_segments'] == 6
    rows = _by_time(updated)
    assert rows['02:20']['reason_code'] == 'BALANCE_FAIL'
    assert Decimal(rows['02:20']['claimable_kwh']) == 0
    assert all(r['basis_revision'] == 1 for r in updated['storage_segments'])
    after = {r['start']: r['claimable_kwh'] for r in updated['storage_segments']}
    assert before != after
    # 上游依据 r2：恢复 250，可申报电量回到初版数字，但版本保留为 r2
    basis.submit_basis(db, rid, {'export_limit_kw': '250'}, note='恢复旧版限值')
    restored = store.get(db, rid)
    rows2 = _by_time(restored)
    assert rows2['02:20']['reason_code'] == 'GRID_CURTAIL'
    assert Decimal(rows2['02:20']['claimable_kwh']) == Decimal('5.0000')
    assert all(r['basis_revision'] == 2 for r in restored['storage_segments'])
    # 原始现场凭据未被删除或改写
    assert len(restored['records']) == len(saved['records'])
    assert [e['revision'] for e in basis.list_basis(db, rid)] == [1, 2]


def test_basis_validation_failure_keeps_old_version(tmp_path):
    db = str(tmp_path / 'd.sqlite')
    saved = store.save(db, evaluate({'dataset': 'storage'}))
    rid = saved['manifest']['run_id']
    basis.submit_basis(db, rid, {'export_limit_kw': '210'}, note='v1')
    with pytest.raises(BusinessError) as exc:
        basis.submit_basis(db, rid, {'export_limit_kw': '-10'})
    assert exc.value.code == 'BAD_BASIS'
    with pytest.raises(BusinessError):
        basis.submit_basis(db, rid, {'unknown_field': '1'})
    with pytest.raises(BusinessError):
        basis.submit_basis(db, rid, {})
    entries = basis.list_basis(db, rid)
    assert len(entries) == 1 and entries[0]['revision'] == 1
    # 现场结果与原始记录未被破坏
    again = store.get(db, rid)
    assert again['storage_segments'][0]['basis_revision'] == 1
    assert len(again['records']) == len(saved['records'])

def test_basis_rejected_on_pv_only_run(tmp_path, clean):
    db = str(tmp_path / 'e.sqlite')
    saved = store.save(db, clean)
    with pytest.raises(BusinessError) as exc:
        basis.submit_basis(db, saved['manifest']['run_id'], {'charge_kw': '40'})
    assert exc.value.code == 'NO_STORAGE'


def test_switch_back_restores_prior_numbers(tmp_path):
    db = str(tmp_path / 'f.sqlite')
    saved = store.save(db, evaluate({'dataset': 'storage'}))
    rid = saved['manifest']['run_id']
    original_claim = saved['storage_summary']['claimable_kwh']
    basis.submit_basis(db, rid, {'export_limit_kw': '290'}, note='放宽')
    changed = store.get(db, rid)
    assert changed['storage_summary']['claimable_kwh'] != original_claim
    # 切回旧版限值（新依据内容等同初始上下文），电量回到旧版数字
    basis.submit_basis(db, rid, {'export_limit_kw': '250'}, note='恢复旧版')
    restored = store.get(db, rid)
    assert restored['storage_summary']['claimable_kwh'] == original_claim
    assert [e['revision'] for e in basis.list_basis(db, rid)] == [1, 2]


def test_charge_basis_overrides_battery_power(tmp_path):
    db = str(tmp_path / 'g.sqlite')
    saved = store.save(db, evaluate({'dataset': 'storage'}))
    rid = saved['manifest']['run_id']
    # 10:10 原遥测充电 40kW，三方平衡（NO_LOSS）；上游依据更正为充电 80kW，
    # 自然净出口降到 170kW，实测仍 210kW -> 实测高于自然净出口，留待查
    out = basis.submit_basis(db, rid, {'charge_kw': '80'}, note='充电功率更正')
    assert out['changed'] and out['basis_revision'] == 1
    updated = store.get(db, rid)
    row = _by_time(updated)['02:10']
    assert Decimal(row['charge_kw']) == Decimal('80')
    assert Decimal(row['charge_kwh']).quantize(Decimal('.0001')) == Decimal('13.3333')
    assert row['battery_source'] == 'basis'
    assert row['reason_code'] == 'OVER_DELIVER' and row['review_flag']
    # 10:50 槽位电池有遥测、表计缺测：电池来源可被依据覆盖，待查只来自表计缺口
    gap_row = _by_time(updated)['02:50']
    assert gap_row['battery_source'] == 'basis'
    assert gap_row['export_kw'] is None and gap_row['review_flag']
    # 现场遥测记录本身未被改写
    assert next(r for r in updated['records'] if r['id'] == 'BESS-10')['power_kw'] == '-40'


def test_discharge_basis_shrinks_grid_curtailment(tmp_path):
    db = str(tmp_path / 'h.sqlite')
    saved = store.save(db, evaluate({'dataset': 'storage'}))
    rid = saved['manifest']['run_id']
    before = _by_time(saved)['02:20']
    assert before['reason_code'] == 'GRID_CURTAIL'
    assert Decimal(before['discharge_kw']) == Decimal('30')
    # 上游依据把放电更正为 20kW：自然净出口由 280 降到 270，实测仍压在限值 250，
    # 约束缺口由 5kWh 缩小为 (270-250)/6 = 3.3333kWh，仍可申报
    basis.submit_basis(db, rid, {'discharge_kw': '20'}, note='放电更正')
    row = _by_time(store.get(db, rid))['02:20']
    assert row['battery_source'] == 'basis'
    assert Decimal(row['discharge_kw']) == Decimal('20')
    assert Decimal(row['discharge_kwh']).quantize(Decimal('.0001')) == Decimal('3.3333')
    assert row['reason_code'] == 'GRID_CURTAIL'
    assert Decimal(row['claimable_kwh']).quantize(Decimal('.0001')) == Decimal('3.3333')
    # 净差法对照值不变（pv 与实测未变），但可申报量随电池依据变化
    assert Decimal(row['naive_claim_kwh']) == 0


def test_charge_and_discharge_together_rejected(tmp_path):
    db = str(tmp_path / 'i.sqlite')
    saved = store.save(db, evaluate({'dataset': 'storage'}))
    rid = saved['manifest']['run_id']
    with pytest.raises(BusinessError) as exc:
        basis.submit_basis(db, rid, {'charge_kw': '10', 'discharge_kw': '5'})
    assert exc.value.code == 'BAD_BASIS'
    assert basis.list_basis(db, rid) == []


def test_basis_over_capacity_rejected(tmp_path):
    db = str(tmp_path / 'j.sqlite')
    saved = store.save(db, evaluate({'dataset': 'storage'}))
    rid = saved['manifest']['run_id']
    # BESS 额定 100kW，1.2 倍上限 120
    with pytest.raises(BusinessError) as exc:
        basis.submit_basis(db, rid, {'charge_kw': '121'})
    assert exc.value.code == 'BAD_BASIS'
