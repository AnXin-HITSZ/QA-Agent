from app.config import Settings
from app.memory.graph.base import resolve_switches
from scripts import run_memory_eval as runner
from types import SimpleNamespace


def test_graph_is_only_memory_boolean_configuration():
    switches={name for name,field in Settings.model_fields.items() if name.startswith('memory_') and field.annotation is bool}
    assert switches=={'memory_graph_enabled'}
    assert 'rerank_enabled' not in Settings.model_fields
    s=Settings(_env_file=None)
    assert s.memory_source_retention_days==0 and s.memory_evidence_rounds==0


def test_legacy_subswitches_cannot_disable_graph_master():
    s=Settings(_env_file=None,memory_graph_enabled=True,memory_graph_write_enabled=False,
               memory_graph_search_enabled=False,memory_graph_worker_enabled=False)
    sw=resolve_switches(s)
    assert not sw.write and not sw.search and sw.worker


def test_cli_experiment_cannot_enable_disabled_graph_and_restores_state():
    off=Settings(_env_file=None,memory_graph_enabled=False)
    on=Settings(_env_file=None,memory_graph_enabled=True)
    assert not resolve_switches(on).search
    assert not resolve_switches(off).search


def test_runner_forwards_graph_ablation_without_environment_flags():
    args=SimpleNamespace(dataset='data.json',context_chars=100000,no_judge=True,variant='off',action='ask',graph_recall='disabled')
    commands=runner.plan(args,'run')
    assert '--graph-recall' not in commands[0]
