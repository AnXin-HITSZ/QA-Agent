from tests import migrationkit as mig


def test_file_search_migration_matches_models():
    schema = mig.migration_tables()
    for name, model in mig.model_tables("app.rag.search_repo").items():
        mig.compare_tables(schema[name], model, label=name)
