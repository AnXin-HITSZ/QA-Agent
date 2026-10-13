"""Guarded developer migration and schema check; never changes a production DB."""
import argparse,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.run_memory_eval import settings_and_environment
from sqlalchemy import create_engine,inspect,text
from app.matters.tables import Base

def main():
    p=argparse.ArgumentParser();p.add_argument('--apply-dev-migration',action='store_true');args=p.parse_args()
    s=settings_and_environment();engine=create_engine(s.mysql_url)
    try:
        with engine.connect() as conn:
            name=conn.execute(text('SELECT DATABASE()')).scalar()
            if args.apply_dev_migration:
                if s.app_env!='dev' or name!='qa_agent_dev':raise RuntimeError('拒绝在开发库之外执行迁移')
                existing=set(inspect(conn).get_table_names())
                sql=(Path(__file__).resolve().parents[1]/'migrations/0011_administrative_matters.up.sql').read_text(encoding='utf-8')
                sql='\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
                for statement in sql.split(';'):
                    if not statement.strip():continue
                    table=statement.strip().split()[2]
                    if table not in Base.metadata.tables:raise RuntimeError('迁移表不在事务模型中')
                    if table not in existing:conn.execute(text(statement));conn.commit()
            inspector=inspect(conn)
            for name,table in Base.metadata.tables.items():
                actual={c['name'] for c in inspector.get_columns(name)}
                if actual!={c.name for c in table.columns}:raise RuntimeError('事务表字段不匹配')
        print('事务表检查通过');return 0
    except Exception as exc:
        code=getattr(getattr(exc,'orig',None),'args',(None,))[0]
        print('事务表检查失败：'+type(exc).__name__+' code='+str(code),file=sys.stderr);return 1
    finally:engine.dispose()
if __name__=='__main__':raise SystemExit(main())
