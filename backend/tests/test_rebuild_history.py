"""Mixed policies, rollback, missing history and permanent SQL audit."""
import json
import pytest
from app.rag import ingest, store, rebuild, job_repo, ocr_jobs
from app.schemas.knowledge import RebuildOverride


def test_mixed_rebuild_publishes_once_and_uses_active_version_after_rollback(kb_env, monkeypatch):
    kb_env.kb.put_object('a.md', b'# alpha\n\nEquipment approval by Alice.')
    kb_env.kb.put_object('b.md', b'# beta\n\nEquipment return by Bob.')
    first=ingest.run_index_job(kb_env.kb, ingest.Scope(), ingest.Options(), file_options={
        'a.md':{'extraction_mode':'general','mixed_invoice':False},
        'b.md':{'extraction_mode':'invoice','mixed_invoice':True}})
    assert first['published']
    preview=rebuild.plan(ingest.Scope())
    assert preview['unknown']==0
    assert preview['groups']=={'general':1,'invoice+mixed_invoice':1}
    assert all(not row['extraction_options']['refresh_ocr'] for row in preview['files'])
    selected=rebuild.resolve(preview, [])
    selected['a.md']['extraction_mode']='general_advanced'
    second=ingest.run_index_job(kb_env.kb, ingest.Scope(), ingest.Options(), file_options=selected,
        expected_active=preview['active'],expected_sources=preview['files'])
    assert second['published'] and second['indexed_files']==2
    assert rebuild.plan(ingest.Scope())['groups']['general_advanced']==1
    store.rollback()
    assert rebuild.plan(ingest.Scope())['groups']=={'general':1,'invoice+mixed_invoice':1}


def test_unknown_blocks_and_explicit_override_does_not_refresh_ocr(kb_env):
    kb_env.kb.put_object('new.md', b'new file')
    kb_env.kb.put_object('archive.zip', b'unsupported')
    preview=rebuild.plan(ingest.Scope())
    assert preview['unknown']==1 and preview['unsupported']==1
    with pytest.raises(ValueError, match='缺少'):
        rebuild.resolve(preview, [])
    chosen=rebuild.resolve(preview,[RebuildOverride(key='new.md',options={'extraction_mode':'general'})])
    assert not chosen['new.md']['refresh_ocr']
    with pytest.raises(ValueError,match='范围'):
        rebuild.resolve(preview,[RebuildOverride(key='elsewhere.md',options={'extraction_mode':'native_only'})])


def test_source_change_rejects_before_paid_work(kb_env,monkeypatch):
    kb_env.kb.put_object('a.md',b'old')
    preview=rebuild.plan(ingest.Scope())
    kb_env.kb.put_object('a.md',b'new contents')
    monkeypatch.setattr(ingest,'get_embeddings',lambda:pytest.fail('paid preflight'))
    with pytest.raises(RuntimeError,match='原件已变化'):
        ingest.run_index_job(kb_env.kb,ingest.Scope(),ingest.Options(),expected_sources=preview['files'])


def test_permanent_history_over_50_import_idempotent_and_no_pending_cleanup(kb_env):
    for i in range(55):
        ocr_jobs._write({'job_id':f'job-{i:03}', 'status':'published','created_at':f'2026-10-14T00:00:{i:02}Z'})
    ocr_jobs._prune()
    assert job_repo.list_page(0,100)['total']==55
    directory=ocr_jobs.jobs_dir();directory.mkdir(parents=True,exist_ok=True)
    (directory/'old.json').write_text(json.dumps({'job_id':'old','status':'published','created_at':'2020'}))
    (directory/'old.files.jsonl').write_text(json.dumps({'key':'old.md','status':'indexed'})+'\n')
    assert job_repo.import_legacy(directory)==1
    assert job_repo.import_legacy(directory)==0
    assert job_repo.files('old')['total']==1
    assert job_repo.list_page(0,2)['items'][0]['job_id']=='job-054'


def test_legacy_manifest_must_match_actual_chunk_generation(kb_env):
    from app.rag import documents
    kb_env.kb.put_object('a.md',b'# old\n\nOriginal content.')
    first=ingest.run_index_job(kb_env.kb,ingest.Scope(),ingest.Options(extraction_mode='general'))
    kb_env.kb.put_object('a.md',b'# new\n\nChanged content.')
    ingest.run_index_job(kb_env.kb,ingest.Scope(),ingest.Options(extraction_mode='invoice'))
    # Redis now describes the later version. It cannot define the rolled-back file.
    assert 'a.md' not in rebuild.legacy_policies(first['index_version'])


def test_sql_failure_releases_new_job_lock(kb_env,monkeypatch):
    def fail(_):
        raise RuntimeError('database down')
    monkeypatch.setattr(job_repo,'write',fail)
    with pytest.raises(RuntimeError):
        ocr_jobs.new_job(ingest.Scope(),ingest.Options())
    assert ocr_jobs.lock_owner() is None


def test_other_live_worker_is_not_interrupted_by_startup(kb_env,monkeypatch):
    job=ocr_jobs.new_job(ingest.Scope(),ingest.Options())
    monkeypatch.setattr(ocr_jobs,'_other_worker_alive',lambda _:True)
    assert ocr_jobs.reconcile()==0
    assert ocr_jobs.get_job(job['job_id'])['status']=='queued'


def test_reuploaded_same_path_does_not_inherit_old_identity_policy(kb_env):
    from app.rag import documents
    kb_env.kb.put_object('a.md',b'# document\n\nContent.')
    ingest.run_index_job(kb_env.kb,ingest.Scope(),ingest.Options(extraction_mode='general'))
    kb_env.cache.delete([documents.path_key('a.md')])
    documents.register('a.md',content_sha256='new-upload')
    assert rebuild.plan(ingest.Scope())['unknown']==1


def test_restart_after_publication_preserves_published_state(kb_env):
    from app.rag import documents
    from app.rag.localfs import atomic_write_json
    job=ocr_jobs.new_job(ingest.Scope(),ingest.Options())
    atomic_write_json(documents.publish_record_path(job['job_id']),
                      {'status':'applied','index_version':'committed-v1'})
    assert ocr_jobs.reconcile()==1
    recovered=ocr_jobs.get_job(job['job_id'])
    assert recovered['published'] and recovered['status']=='published'


def test_audit_errors_are_bounded_and_redacted(kb_env):
    job_repo.write({'job_id':'error','status':'failed','error':'api_key=secret-key '+ 'x'*1000})
    error=job_repo.get('error')['error']
    assert 'secret-key' not in error and len(error)<=500
