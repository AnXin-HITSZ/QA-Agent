"""Persisted chat proposals, explicit user confirmation, one atomic business commit."""
import hashlib
import json
import uuid
from datetime import timedelta
from sqlalchemy import select
from pydantic import ValidationError
from app.matters import service, schemas
from app.matters.tables import Proposal
from app.memory import db

MODELS = {'create': schemas.Create, 'edit': schemas.Edit, 'event': schemas.Action,
          'link': schemas.LinkInput, 'edit_link': schemas.LinkInput,
          'unlink': schemas.Delete, 'delete': schemas.Delete}


def view(row):
    return {key: getattr(row, key) for key in ('id', 'matter_id', 'operation', 'payload',
            'before', 'status', 'result', 'created_at', 'expires_at')}


def propose(uid, thread_id, operation, payload, matter_id='', link_id=''):
    if not uid or not thread_id:
        raise service.Invalid('缺少已认证的会话身份')
    if operation not in MODELS:
        raise service.Invalid('不支持的事务操作')
    if operation == 'create' and matter_id:
        raise service.Invalid('新建事项不能指定旧事项ID')
    pid = uuid.uuid4().hex
    with db.session_scope() as session:
        before = {}
        data = dict(payload)
        # Request identity and expected version are server controlled.
        data.pop('request_id', None)
        data.pop('revision', None)
        if operation != 'create':
            # Read a snapshot only; confirmation takes the business lock and checks this version.
            row = service._row(session, uid, matter_id)
            before = json.loads(json.dumps(service._view(row), default=str))
            data['revision'] = row.revision
            if operation == 'edit':
                data['title'] = data.get('title', row.title)
                data['details'] = {**row.details, **data.get('details', {})}
        if operation in ('edit_link', 'unlink'):
            link = next((x for x in service._links(session, uid, matter_id) if x.id == link_id), None)
            if not link:
                raise service.NotFound()
            before['link'] = service._link_view(link)
        data['request_id'] = 'proposal:' + pid
        try:
            request = MODELS[operation].model_validate(data)
        except ValidationError as exc:
            raise service.Invalid('字段不完整或无效：' + '; '.join(
                '.'.join(map(str, e['loc'])) + ': ' + e['msg'] for e in exc.errors())) from None
        if operation in ('create', 'edit'):
            details = request.details.model_dump(mode='json')
            service._requirements(details)
            missing = [name for name in ('amount', 'purpose') if not details.get(name)]
            if missing:
                raise service.Invalid('请先向用户询问缺失字段：' + ', '.join(missing))
        clean = request.model_dump(mode='json')
        signature = {k: v for k, v in clean.items() if k != 'request_id'}
        key = hashlib.sha256(json.dumps([thread_id, matter_id, link_id, operation, signature],
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        old = session.scalar(select(Proposal).where(Proposal.user_id == uid, Proposal.dedupe_key == key).with_for_update())
        if old:
            if old.status == 'confirmed' or (old.status == 'pending' and old.expires_at > db.utc_naive()):
                return view(old)
            # Keep the audit record while allowing a new explicit proposal after rejection/expiry.
            old.dedupe_key = hashlib.sha256((key + old.id).encode()).hexdigest()
            if old.status == 'pending':
                old.status = 'expired'
            session.flush()
        # Revised proposals replace previous pending operations for this same target.
        for old in session.scalars(select(Proposal).where(Proposal.user_id == uid,
                Proposal.thread_id == thread_id, Proposal.matter_id == matter_id,
                Proposal.operation == operation, Proposal.status == 'pending').with_for_update()):
            old_request = old.payload.get('request', {})
            same_object = operation not in ('create', 'link') or (
                old_request.get('title') == clean.get('title') and
                old_request.get('kind') == clean.get('kind'))
            if same_object and old.payload.get('link_id', '') == link_id:
                old.status = 'superseded'
        stored = {'request': clean, 'link_id': link_id}
        row = Proposal(id=pid, user_id=uid, thread_id=thread_id, matter_id=matter_id,
                operation=operation, payload=stored, before=before, dedupe_key=key,
                status='pending', result={}, created_at=db.utc_naive(),
                expires_at=db.utc_naive() + timedelta(hours=24))
        session.add(row)
        session.flush()
        return view(row)


def listing(uid, thread_id):
    with db.session_scope() as session:
        return [view(x) for x in session.scalars(select(Proposal).where(
            Proposal.user_id == uid, Proposal.thread_id == thread_id).order_by(
            Proposal.created_at.desc(), Proposal.id).limit(30))]


def decide(uid, pid, confirm):
    with db.session_scope() as session:
        row = session.scalar(select(Proposal).where(Proposal.id == pid,
            Proposal.user_id == uid).with_for_update())
        if not row:
            raise service.NotFound()
        if row.status == 'confirmed':
            if not confirm:
                raise service.Conflict('已确认的操作不能通过拒绝撤销，请编辑事项')
            return view(row)
        if row.status != 'pending':
            if row.status == 'rejected' and not confirm:
                return view(row)
            raise service.Conflict('操作已拒绝或已被更新，请重新生成')
        if not confirm:
            row.status = 'rejected'
            return view(row)
        if row.expires_at <= db.utc_naive():
            raise service.Conflict('待确认操作已过期，请重新生成')
        request = MODELS[row.operation].model_validate(row.payload['request'])
        if row.operation == 'create':
            result = service._create(uid, request, session=session)
        elif row.operation == 'edit_link':
            result = service.edit_link(uid, row.matter_id, row.payload['link_id'], request, session=session)
        elif row.operation == 'unlink':
            result = service.remove_link(uid, row.matter_id, row.payload['link_id'], request, session=session)
        else:
            result = service.mutate(uid, row.matter_id, request, row.operation, session=session)
        row.status = 'confirmed'
        row.matter_id = result['id']
        row.result = json.loads(json.dumps(result, default=str))
        return view(row)
