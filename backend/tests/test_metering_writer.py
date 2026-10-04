"""写入器:入队 → 批量入库、数据库故障落补写目录、恢复后补写、幂等不重复计数。

用内存仓库替代 MySQL,但走的是真实的队列 / 补写 / 幂等代码路径;不连任何数据库。
"""

from __future__ import annotations

import logging
import queue
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.metering import build_call, record_call
from app.metering import pending as pending_mod
from app.metering.model import (
    COST_ESTIMATED, COST_UNKNOWN, NOTE_PRICE_NOT_LOADED, SERVICE_EMBEDDING, SERVICE_OCR,
    STATUS_FAILURE, STATUS_SUCCESS, USAGE_SOURCE_VENDOR, CallItem, CacheEvent,
)
from app.metering.store import CallFilter

UTC = timezone.utc


def _embedding_call(**over) -> object:
    kwargs = dict(service=SERVICE_EMBEDDING, provider="dashscope", target="text-embedding-v4",
                  endpoint="dashscope.aliyuncs.com", duration_ms=100, status=STATUS_SUCCESS,
                  usage_quantity=Decimal(1000), usage_unit="token",
                  usage_source=USAGE_SOURCE_VENDOR, price_unit="1k_tokens")
    kwargs.update(over)
    return build_call(**kwargs)


def _events(store) -> list[dict]:
    return store.list_calls(CallFilter(), offset=0, limit=100)["items"]


# ---- 队列 → 数据库 ----

def test_flush_writes_queued_events_once(metering_env):
    for _ in range(3):
        record_call(_embedding_call())
    assert len(_events(metering_env.store)) == 0        # 还没冲刷:业务线程只入队

    result = metering_env.writer.flush()
    assert result["written"] == 3
    assert len(_events(metering_env.store)) == 3
    assert metering_env.writer.status()["queued"] == 0
    assert metering_env.writer.status()["db_ok"] is True


def test_flush_batches_do_not_lose_or_duplicate(metering_env, monkeypatch):
    monkeypatch.setattr(metering_env.settings, "metering_flush_batch", 2)
    for _ in range(5):
        record_call(_embedding_call())
    written = 0
    for _ in range(3):                                   # 每轮最多 2 条
        written += metering_env.writer.flush()["written"]
    assert written == 5
    rows = _events(metering_env.store)
    assert len(rows) == 5
    assert len({r["event_id"] for r in rows}) == 5       # 没有重复行


def test_cache_events_go_to_their_own_table(metering_env):
    from app.metering import record_cache

    record_cache(CacheEvent(event_id="a" * 32, layer="embedding", purpose="document_index",
                            unit="text", hit_count=2, miss_count=1))
    metering_env.writer.flush()
    assert list(metering_env.store.cache_events) == ["a" * 32]


def test_items_are_written_with_allocation(metering_env):
    metering_env.store.add_price({
        "service": SERVICE_OCR, "provider": "aliyun", "target": "", "unit": "request",
        "currency": "CNY", "unit_price": Decimal("0.05"),
        "effective_from": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "source": "控制台 2026-10-03", "note": "",
    })
    metering_env.writer.price_book.rules(refresh=True)

    record_call(build_call(service=SERVICE_OCR, provider="aliyun", target="Invoice",
                           endpoint="ocr.aliyuncs.com", duration_ms=200, status=STATUS_SUCCESS,
                           usage_quantity=Decimal(1), usage_unit="request",
                           price_unit="request",
                           items=(CallItem(oss_key="a.pdf", page_no=1, text_count=1),)))
    metering_env.writer.flush()
    row = _events(metering_env.store)[0]
    detail = metering_env.store.get_call(row["event_id"])
    assert detail["cost_amount"] == "0.05000000"
    assert detail["items"][0]["oss_key"] == "a.pdf"
    assert detail["items"][0]["allocated_cost"] == "0.05000000"
    assert "估算" in detail["items"][0]["allocation_note"]


# ---- 数据库故障 → 补写目录 ----

def test_db_failure_spills_to_pending_then_backfills(metering_env):
    metering_env.store.fail_writes = True
    record_call(_embedding_call())

    assert metering_env.writer.flush()["written"] == 0
    assert _events(metering_env.store) == []
    files = pending_mod.pending_files(metering_env.pending)
    assert len(files) == 1                               # 落盘,不丢
    status = metering_env.writer.status()
    assert status["db_ok"] is False and status["spilled"] == 1 and status["pending"] == 1
    assert "MemoryStore" in status["db_error"]

    metering_env.store.fail_writes = False               # 数据库恢复
    assert metering_env.writer.flush()["backfilled"] == 1
    assert len(_events(metering_env.store)) == 1
    assert pending_mod.pending_files(metering_env.pending) == []
    assert metering_env.writer.status()["pending"] == 0


def test_backfill_is_idempotent_on_replay(metering_env, monkeypatch):
    """补写重放不会重复计数:同一条记录写两次,库里仍只有一行。"""
    metering_env.store.fail_writes = True
    record_call(_embedding_call())
    metering_env.writer.flush()
    metering_env.store.fail_writes = False

    original = pending_mod.pending_files(metering_env.pending)[0]
    payload = pending_mod.read_payload(original)
    assert payload is not None

    assert metering_env.writer.flush()["backfilled"] == 1
    # 模拟「提交成功但删除补写文件前崩溃」:同一个文件再来一次
    assert pending_mod.write(payload, directory=metering_env.pending) is not None
    metering_env.writer.flush()

    assert len(_events(metering_env.store)) == 1          # 幂等:仍然一行
    assert metering_env.writer.status()["backfilled"] == 1


def test_restart_picks_up_pending_files(metering_env):
    """进程重启后补写目录里的记录仍然会被补上(持久、跨重启)。"""
    from app.metering.writer import MeteringWriter, install_writer

    metering_env.store.fail_writes = True
    record_call(_embedding_call())
    metering_env.writer.flush()
    metering_env.store.fail_writes = False

    class _Restarted(MeteringWriter):
        def _ensure_thread(self) -> None:
            return

    fresh = _Restarted()                                 # 新进程 / 新写入器
    install_writer(fresh)
    try:
        assert fresh.flush()["backfilled"] == 1
    finally:
        install_writer(metering_env.writer)
    assert len(_events(metering_env.store)) == 1


def test_backfill_backs_off_while_db_is_down(metering_env):
    """退避期间不反复敲打故障中的数据库;恢复后仍能补上。"""
    metering_env.store.fail_writes = True
    record_call(_embedding_call())
    metering_env.writer.flush()

    metering_env.store.fail_writes = False
    assert metering_env.writer._backfill() == 0          # 退避中:本轮不试 noqa: SLF001
    metering_env.writer._retry_at = 0.0                  # noqa: SLF001 —— 模拟退避到点
    assert metering_env.writer._backfill() == 1          # noqa: SLF001


def test_spill_failure_is_counted_and_logged_not_silent(metering_env, monkeypatch, caplog):
    """连补写文件都写不下(磁盘故障):告警 + 计数,不静默丢。"""
    monkeypatch.setattr(pending_mod, "write", lambda payload, **kw: None)
    metering_env.store.fail_writes = True

    with caplog.at_level(logging.ERROR, logger="app.metering.writer"):
        record_call(_embedding_call())
        metering_env.writer.flush()

    assert metering_env.writer.status()["lost"] == 1
    assert any("未能保存" in r.message for r in caplog.records)


def test_full_queue_spills_instead_of_blocking_business(metering_env, monkeypatch):
    """队列满时直接落盘:业务线程不阻塞、不丢记录。"""
    metering_env.writer._queue = queue.Queue(maxsize=1)   # noqa: SLF001
    record_call(_embedding_call())
    record_call(_embedding_call())
    assert metering_env.writer.status()["spilled"] == 1
    assert len(pending_mod.pending_files(metering_env.pending)) == 1


def test_corrupt_pending_file_is_kept_not_deleted(metering_env, caplog):
    bad = metering_env.pending / "broken.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{ this is not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.metering.writer"):
        assert metering_env.writer.flush()["backfilled"] == 0
    assert bad.exists()                                   # 保留待人工处理
    assert any("无法解析" in r.message for r in caplog.records)


# ---- 补价:按事件发生时刻的价,不按补写时刻的价 ----

def test_requote_uses_price_effective_at_event_time(metering_env):
    """记录时价格表未载入 → 写库前补价;补价用**事件发生时刻**的规则。"""
    when = datetime.now(UTC) - timedelta(days=1)
    event = _embedding_call(occurred_at=when)
    assert event.cost_status == COST_UNKNOWN and event.cost_note == NOTE_PRICE_NOT_LOADED

    metering_env.store.add_price({                        # 事件之后才生效的涨价规则
        "service": SERVICE_EMBEDDING, "provider": "dashscope", "target": "",
        "unit": "1k_tokens", "currency": "CNY", "unit_price": Decimal("0.001"),
        "effective_from": datetime.now(UTC).isoformat(), "source": "", "note": "",
    })
    metering_env.store.add_price({                        # 事件之前生效的规则:应当用这条
        "service": SERVICE_EMBEDDING, "provider": "dashscope", "target": "",
        "unit": "1k_tokens", "currency": "CNY", "unit_price": Decimal("0.000514"),
        "effective_from": (when - timedelta(days=10)).isoformat(), "source": "", "note": "",
    })
    metering_env.writer.price_book.invalidate()
    record_call(event)
    metering_env.writer.flush()

    row = _events(metering_env.store)[0]
    assert row["cost_status"] == COST_ESTIMATED
    assert row["cost_amount"] == "0.00051400"             # 用老价,不被后来的涨价影响
    assert row["price_snapshot"]["unit_price"] == "0.000514"


def test_missing_price_stays_unknown_after_flush(metering_env):
    """真的没配价格:如实记「无法估算」,不写 0。"""
    record_call(_embedding_call())
    metering_env.writer.flush()
    row = _events(metering_env.store)[0]
    assert row["cost_amount"] is None and row["cost_status"] == COST_UNKNOWN
    assert row["cost_note"]


def test_stored_event_is_not_recomputed_when_price_changes(metering_env):
    """改价不回溯:库里已有的行原样不动。"""
    metering_env.store.add_price({
        "service": SERVICE_EMBEDDING, "provider": "dashscope", "target": "",
        "unit": "1k_tokens", "currency": "CNY", "unit_price": Decimal("0.000514"),
        "effective_from": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
        "source": "", "note": "",
    })
    metering_env.writer.price_book.rules(refresh=True)
    record_call(_embedding_call())
    metering_env.writer.flush()
    before = _events(metering_env.store)[0]

    metering_env.store.add_price({                        # 事后涨价
        "service": SERVICE_EMBEDDING, "provider": "dashscope", "target": "",
        "unit": "1k_tokens", "currency": "CNY", "unit_price": Decimal("0.009"),
        "effective_from": datetime.now(UTC).isoformat(), "source": "", "note": "",
    })
    metering_env.writer.price_book.invalidate()
    metering_env.writer.flush()

    after = metering_env.store.get_call(before["event_id"])
    assert after["cost_amount"] == before["cost_amount"]
    assert after["price_version"] == before["price_version"]


# ---- 计量失败不影响业务 ----

def test_record_call_never_raises_even_if_writer_is_broken(metering_env, monkeypatch):
    def boom(_event):
        raise RuntimeError("写入器炸了")

    monkeypatch.setattr(metering_env.writer, "call", boom)
    record_call(_embedding_call())                       # 不抛异常 = 业务不受影响


def test_record_call_is_noop_when_metering_disabled(metering_env, monkeypatch):
    monkeypatch.setattr(metering_env.settings, "metering_mysql_url", "")
    record_call(_embedding_call())
    assert metering_env.writer.status()["queued"] == 0


def test_failed_call_is_recorded_without_usage_or_cost(metering_env):
    record_call(_embedding_call(status=STATUS_FAILURE, usage_quantity=None,
                                usage_source="unknown", error_class="APITimeoutError",
                                error_message="请求超时 token=secret"))
    metering_env.writer.flush()
    row = _events(metering_env.store)[0]
    assert row["status"] == STATUS_FAILURE
    assert row["usage_quantity"] is None and row["cost_amount"] is None
    assert row["error_class"] == "APITimeoutError"
    assert "secret" not in row["error_message"]           # 脱敏在写库前完成
