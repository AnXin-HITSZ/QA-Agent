"""Explicit, bounded cleanup of retired administrative Neo4j objects; no MySQL writes."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TARGET = '(n:QAAdministrativeMatter OR n:QAAdministrativeObject)'
SCHEMA = {
    'qa_administrative_matter_identity': ('constraint', ['QAAdministrativeMatter'], ['user_id', 'id']),
    'qa_administrative_object_identity': ('constraint', ['QAAdministrativeObject'], ['user_id', 'id']),
    'qa_administrative_objects_owner': ('index', ['QAAdministrativeObject'], ['user_id', 'matter_id']),
}


def inspect_graph(session):
    def count(query):
        return session.run(query).single()['n']
    return {
        'administrative_nodes': count('MATCH (n) WHERE '+TARGET+' RETURN count(n) AS n'),
        'administrative_edges': count('MATCH ()-[r:QA_ADMIN_LINK]->() RETURN count(r) AS n'),
        'protected_memory_nodes': count('MATCH (n:QAMemoryGraph) RETURN count(n) AS n'),
        'mixed_labels': count('MATCH (n:QAMemoryGraph) WHERE '+TARGET+' RETURN count(n) AS n'),
        'cross_boundary_edges': count('MATCH (n)-[r]-(m) WHERE '+TARGET+
            ' AND NOT (m:QAAdministrativeMatter OR m:QAAdministrativeObject) RETURN count(r) AS n'),
        'unowned_administrative_edges': count('MATCH (n)-[r:QA_ADMIN_LINK]->(m) WHERE NOT '+TARGET+
            ' OR NOT (m:QAAdministrativeMatter OR m:QAAdministrativeObject) RETURN count(r) AS n'),
    }


def schema_objects(session):
    found = {}
    for kind, query in [('constraint', 'SHOW CONSTRAINTS'), ('index', 'SHOW INDEXES')]:
        for record in session.run(query):
            name = record['name']
            if name not in SCHEMA:
                continue
            expected_kind, labels, properties = SCHEMA[name]
            # Constraints own backing indexes with the same name; remove only the constraint.
            if kind != expected_kind:
                if kind == 'index' and record.get('owningConstraint') == name:
                    continue
                raise RuntimeError('Unexpected schema object kind: '+name)
            if list(record['labelsOrTypes']) != labels or list(record['properties']) != properties:
                raise RuntimeError('Schema identity mismatch: '+name)
            found[name] = kind
    return found


def cleanup(session, *, apply=False, batch_size=500):
    before = inspect_graph(session)
    objects = schema_objects(session)
    if before['mixed_labels'] or before['cross_boundary_edges'] or before['unowned_administrative_edges']:
        raise RuntimeError('Refusing mixed labels or relationships crossing the retirement boundary')
    removed = 0
    if apply:
        from neo4j import unit_of_work
        @unit_of_work(timeout=30)
        def delete_batch(tx):
            # Recheck the boundary in each transaction; only retired nodes can be deleted.
            checked = inspect_graph(tx)
            if checked['mixed_labels'] or checked['cross_boundary_edges'] or checked['unowned_administrative_edges']:
                raise RuntimeError('Retirement boundary changed during cleanup')
            return tx.run('MATCH (n) WHERE '+TARGET+
                ' WITH n LIMIT $limit DETACH DELETE n RETURN count(n) AS n', limit=batch_size).single()['n']
        while True:
            n = session.execute_write(delete_batch)
            removed += n
            if n == 0:
                break
        for name, kind in objects.items():
            # Names are compile-time allowlisted, never derived from user input.
            session.run('DROP '+('CONSTRAINT' if kind == 'constraint' else 'INDEX')+
                        ' '+name+' IF EXISTS').consume()
    after = inspect_graph(session)
    remaining_schema = schema_objects(session)
    if before['protected_memory_nodes'] != after['protected_memory_nodes']:
        raise RuntimeError('Protected memory count changed; inspect concurrent writers')
    return {'applied': apply, 'before': before, 'after': after, 'removed_nodes': removed,
            'remaining_schema': remaining_schema,
            'clean': after['administrative_nodes'] == 0 and after['administrative_edges'] == 0
                     and not remaining_schema}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true', help='Delete retired administrative graph data/schema')
    p.add_argument('--writers-stopped', action='store_true', help='Confirm all old administrative writers are stopped')
    p.add_argument('--batch-size', type=int, default=500)
    p.add_argument('--output', type=Path)
    args = p.parse_args(argv)
    if args.apply and not args.writers_stopped:
        p.error('--apply requires --writers-stopped')
    if not 1 <= args.batch_size <= 1000:
        p.error('batch-size must be between 1 and 1000')
    from app.config import get_settings
    from neo4j import GraphDatabase
    s = get_settings()
    # Explicit retirement works even while the normal graph feature is disabled.
    with GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_username, s.neo4j_password),
            connection_timeout=s.memory_graph_connect_timeout_seconds,
            connection_acquisition_timeout=s.memory_graph_connect_timeout_seconds,
            max_transaction_retry_time=30) as driver:
        with driver.session(database=s.neo4j_database) as session:
            report = cleanup(session, apply=args.apply, batch_size=args.batch_size)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded)
    return 0 if not args.apply or report['clean'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
