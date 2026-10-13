"""Authenticated personal administrative matters; no role-based privacy bypass."""
from contextlib import contextmanager
from fastapi import APIRouter, Depends, HTTPException, Query
from app.auth.deps import require_user, auth_ready
from app.auth.service import Principal
from app.config import get_settings
from app.memory import db
from app.matters import service, schemas

router=APIRouter(prefix=get_settings().api_prefix+'/matters',tags=['matters'],dependencies=[Depends(auth_ready)])

@contextmanager
def translated():
    try:yield
    except service.NotFound:raise HTTPException(404,'事项不存在或无权访问') from None
    except service.Conflict as exc:raise HTTPException(409,str(exc)) from None
    except service.Invalid as exc:raise HTTPException(422,str(exc)) from None
    except Exception as exc:raise HTTPException(503,'事务服务暂不可用（'+type(exc).__name__+'）') from None

async def call(fn,*args,**kwargs):
    with translated():return await db.run(fn,*args,**kwargs)

@router.get('')
async def listing(status:str='',limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0),user:Principal=Depends(require_user)):
    return {'items':await call(service.list_matters,user.user_id,status=status,limit=limit,offset=offset)}

@router.post('')
async def create(body:schemas.Create,user:Principal=Depends(require_user)):
    return await call(service.create,user.user_id,body)

@router.post('/draft')
async def draft(body:schemas.DraftInput,user:Principal=Depends(require_user)):
    return await call(service.draft,body.text)

@router.get('/{mid}')
async def detail(mid:str,user:Principal=Depends(require_user)):
    return await call(service.get,user.user_id,mid)

@router.patch('/{mid}')
async def edit(mid:str,body:schemas.Edit,user:Principal=Depends(require_user)):
    return await call(service.mutate,user.user_id,mid,body,'edit')

@router.post('/{mid}/events')
async def event(mid:str,body:schemas.Action,user:Principal=Depends(require_user)):
    return await call(service.mutate,user.user_id,mid,body,'event')

@router.post('/{mid}/links')
async def link(mid:str,body:schemas.LinkInput,user:Principal=Depends(require_user)):
    return await call(service.mutate,user.user_id,mid,body,'link')

@router.delete('/{mid}/links/{lid}')
async def unlink(mid:str,lid:str,body:schemas.Delete,user:Principal=Depends(require_user)):
    return await call(service.remove_link,user.user_id,mid,lid,body)

@router.patch('/{mid}/links/{lid}')
async def edit_link(mid:str,lid:str,body:schemas.LinkInput,user:Principal=Depends(require_user)):
    return await call(service.edit_link,user.user_id,mid,lid,body)

@router.delete('/{mid}')
async def delete(mid:str,body:schemas.Delete,user:Principal=Depends(require_user)):
    return await call(service.mutate,user.user_id,mid,body,'delete')

@router.get('/{mid}/graph')
async def graph(mid:str,user:Principal=Depends(require_user)):
    return await call(service.graph,user.user_id,mid)
