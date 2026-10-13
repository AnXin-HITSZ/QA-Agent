import pytest
from scripts import retire_administrative_graph as cli
from tests import migrationkit as mig


class Result(list):
    def single(self): return self[0]
    def consume(self): return None


class Session:
    def __init__(self, nodes=5, cross=0, mixed=0):
        self.nodes=nodes;self.cross=cross;self.mixed=mixed;self.deleted=[]
        self.schema=dict(cli.SCHEMA)
    def run(self, q, **params):
        if q.startswith('SHOW '):
            kind='constraint' if q=='SHOW CONSTRAINTS' else 'index'
            return Result([{'name':name,'labelsOrTypes':labels,'properties':properties}
                for name,(k,labels,properties) in self.schema.items() if k==kind])
        if q.startswith('DROP '):
            self.schema.pop(q.split()[2]);return Result()
        if 'DETACH DELETE' in q:
            n=min(self.nodes,params['limit']);self.nodes-=n;self.deleted.append(n);return Result([{'n':n}])
        if 'NOT (m:' in q and 'MATCH (n)-[r]-' in q:n=self.cross
        elif 'WHERE NOT ' in q:n=0
        elif q.startswith('MATCH (n:QAMemoryGraph) WHERE'):n=self.mixed
        elif q.startswith('MATCH (n:QAMemoryGraph)'):n=7
        elif q.startswith('MATCH ()-'):n=self.nodes
        else:n=self.nodes
        return Result([{'n':n}])
    def execute_write(self, fn):return fn(self)


def test_preview_does_not_write_and_apply_is_bounded_idempotent():
    s=Session()
    assert not cli.cleanup(s)['clean'] and not s.deleted
    report=cli.cleanup(s,apply=True,batch_size=2)
    assert report['clean'] and report['removed_nodes']==5
    assert s.deleted==[2,2,1,0]
    assert report['after']['protected_memory_nodes']==7
    assert cli.cleanup(s,apply=True)['removed_nodes']==0


@pytest.mark.parametrize('kwargs',[{'mixed':1},{'cross':1}])
def test_cross_boundary_nodes_and_edges_prevent_any_deletion(kwargs):
    s=Session(**kwargs)
    with pytest.raises(RuntimeError):cli.cleanup(s,apply=True)
    assert not s.deleted and s.nodes==5


def test_schema_name_cannot_hide_an_unrelated_label():
    s=Session();s.schema['qa_administrative_objects_owner']=('index',['QAMemoryGraph'],['user_id','scope'])
    with pytest.raises(RuntimeError):cli.cleanup(s,apply=True)
    assert not s.deleted


def test_apply_requires_writer_shutdown_acknowledgement():
    with pytest.raises(SystemExit) as e:cli.main(['--apply'])
    assert e.value.code==2


def test_0013_only_retires_five_tables_and_down_restores_structure():
    snapshots={}
    for m in mig.migrations('up'):
        if m.version==13:
            before=dict(snapshots)
            mig.apply_up(snapshots,mig.text(m.path),where=m.path.name)
            assert set(before)-set(snapshots)=={
                'admin_matters','admin_matter_events','admin_matter_links',
                'admin_matter_projections','admin_matter_proposals'}
            down=next(x for x in mig.migrations('down') if x.version==13)
            mig.apply_down(snapshots,mig.text(down.path),where=down.path.name)
            assert snapshots==before
            return
        mig.apply_up(snapshots,mig.text(m.path),where=m.path.name)
    pytest.fail('0013 migration missing')
