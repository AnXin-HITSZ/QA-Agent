from app.graph.nodes import runtime_context
from app.memory import db


def test_enabled_memory_explains_background_write_without_claiming_success(monkeypatch):
    monkeypatch.setattr(db, "enabled", lambda: True)
    text = runtime_context()
    assert "后台自动" in text and "不依赖你调用写入工具" in text
    assert "不得声称已写入成功" in text and "不能承诺定时提醒" in text
    assert "UTC+08:00" in text


def test_unconfigured_memory_does_not_promise_persistence(monkeypatch):
    monkeypatch.setattr(db, "enabled", lambda: False)
    text = runtime_context()
    assert "当前未配置" in text and "不能承诺跨会话保存" in text
    assert "后台自动" not in text


def test_beijing_clock_crosses_utc_date_boundary(monkeypatch):
    from datetime import datetime, timezone
    from app.graph import nodes
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(nodes, "datetime", Clock)
    monkeypatch.setattr(db, "enabled", lambda: True)
    assert "2026-10-14T02:00:00+08:00" in runtime_context()
