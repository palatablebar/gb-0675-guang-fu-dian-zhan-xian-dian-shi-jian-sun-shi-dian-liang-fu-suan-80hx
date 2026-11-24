from fastapi.testclient import TestClient
from app import main,store
from app.engine import evaluate
import pytest

def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'test.sqlite'));return TestClient(main.app)
def test_api_idempotent(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);a=c.post('/api/runs',json={}).json();b=c.post('/api/runs',json={}).json();assert a['manifest']['run_id']==b['manifest']['run_id'];assert len(c.get('/api/runs').json())==1
def test_stale_review(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);a=c.post('/api/runs',json={}).json();url='/api/runs/'+a['manifest']['run_id']+'/review';assert c.post(url,json={'revision':1}).status_code==200;assert c.post(url,json={'revision':1}).status_code==409
def test_seven_entries(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    for k in main.MODULES:
        assert c.post('/api/'+k,json={}).status_code==200
        assert c.get('/'+k).status_code==200
def test_bad_dataset(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);assert c.post('/api/runs',json={'dataset':'../bad'}).status_code==422
def test_missing_run(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);assert c.get('/api/runs/none').status_code==404
def test_restart_store(tmp_path,sample):
    p=str(tmp_path/'durable.sqlite');store.save(p,sample);assert store.get(p,sample['manifest']['run_id'])['manifest']['sha256']==sample['manifest']['sha256']
def test_bad_revision(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);s=c.post('/api/runs',json={}).json();assert c.post('/api/runs/'+s['manifest']['run_id']+'/review',json={'revision':0}).status_code==422

def test_clean_navigation_run_preserved(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=run['manifest']['run_id']
    for k in main.MODULES:
        html=c.get('/'+k+'?run_id='+rid).text
        assert '?run_id='+rid in html
        assert rid in html
    assert c.get('/api/export/'+rid).status_code==200

def test_custom_csv_snapshot_and_navigation(tmp_path,monkeypatch):
    from app.config import ROOT
    c=client(tmp_path,monkeypatch)
    original=(ROOT/'sample_data/clean.csv').read_text()
    # 改成独立于所有仓内样例的证据编号，追踪其跨页与重跑行为。
    first=original.splitlines()[1].split(',')[0]
    custom=original.replace(first,first+'-CUSTOM')
    result=c.post('/api/runs',json={'csv_text':custom}).json()
    rid=result['manifest']['run_id']
    assert '-CUSTOM' in str(result['records']) or '-CUSTOM' in str(result['quarantine'])
    for key in main.MODULES:
        response=c.get('/'+key+'?run_id='+rid)
        assert response.status_code==200
        assert '?run_id='+rid in response.text
    repeated=c.post('/api/runs/'+rid+'/rerun',json={})
    assert repeated.status_code==200
    assert repeated.json()['manifest']['run_id']==rid
    assert repeated.json()['manifest']['sha256']==result['manifest']['sha256']

def test_storage_run_pages_and_basis_api(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={'dataset':'storage'}).json()
    rid=run['manifest']['run_id']
    # 八个模块页面均可访问且带 run_id
    for k in main.MODULES:
        page=c.get('/'+k+'?run_id='+rid)
        assert page.status_code==200 and rid in page.text
    # 储能页展示三类要求信息
    page=c.get('/storage?run_id='+rid).text
    assert '可申报损失' in page and '电池影响' in page and '待查' in page
    # 依据账本初始为空
    assert c.get('/api/runs/'+rid+'/basis').json()['basis']==[]
    # 提交依据生成 r1，同依据重试幂等
    r1=c.post('/api/runs/'+rid+'/basis',json={'basis':{'export_limit_kw':'290'},'note':'放宽'})
    assert r1.status_code==200 and r1.json()['changed'] and r1.json()['basis_revision']==1
    again=c.post('/api/runs/'+rid+'/basis',json={'basis':{'export_limit_kw':'290'}})
    assert again.json()['changed'] is False and again.json()['basis_revision']==1
    assert len(c.get('/api/runs/'+rid+'/basis').json()['basis'])==1
    # 非法依据 422，旧版保留
    bad=c.post('/api/runs/'+rid+'/basis',json={'basis':{'export_limit_kw':'x'}})
    assert bad.status_code==422
    # 导出包含储能仲裁文件
    package=c.get('/api/export/'+rid)
    assert package.status_code==200
    import zipfile,io,json
    z=zipfile.ZipFile(io.BytesIO(package.content))
    assert 'storage_adjudication.json' in z.namelist()
    adj=json.loads(z.read('storage_adjudication.json'))
    assert adj['summary']['segments']==6

def test_basis_api_unknown_run_404(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    assert c.get('/api/runs/nope/basis').status_code==404
    assert c.post('/api/runs/nope/basis',json={'basis':{'charge_kw':'10'}}).status_code==404
