from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
from app import store, basis
from app.errors import BusinessError

router = APIRouter()


def _db():
    from app.main import DB_PATH
    return DB_PATH


@router.get('/storage')
def page(request: Request):
    return render(request, 'storage')


@router.post('/api/storage')
def calculate(payload: dict = Body(default={})):
    return evaluate(payload, until=7)


@router.get('/api/runs/{run_id}/basis')
def basis_history(run_id: str):
    store.get(_db(), run_id)
    return {'basis': basis.list_basis(_db(), run_id)}


@router.post('/api/runs/{run_id}/basis')
def basis_update(run_id: str, payload: dict = Body(...)):
    if not isinstance(payload, dict):
        raise BusinessError('BAD_BASIS', '请求体须为对象')
    return basis.submit_basis(_db(), run_id, payload.get('basis', payload), payload.get('note', ''))
