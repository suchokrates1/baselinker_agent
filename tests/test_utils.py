import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import bl_api_print_agent as bl


def test_shorten_product_name():
    assert bl.shorten_product_name("one two three four") == "one three four"
    assert bl.shorten_product_name("one two") == "one two"


def test_is_quiet_time(monkeypatch):
    class DummyDateTime:
        hour = 0
        @classmethod
        def now(cls):
            return type("obj", (), {"hour": cls.hour})()

    monkeypatch.setattr(bl, "datetime", DummyDateTime)
    monkeypatch.setattr(bl, "QUIET_HOURS_START", 10)
    monkeypatch.setattr(bl, "QUIET_HOURS_END", 22)

    DummyDateTime.hour = 11
    assert bl.is_quiet_time() is True

    DummyDateTime.hour = 23
    assert bl.is_quiet_time() is False

    monkeypatch.setattr(bl, "QUIET_HOURS_START", 22)
    monkeypatch.setattr(bl, "QUIET_HOURS_END", 8)

    DummyDateTime.hour = 23
    assert bl.is_quiet_time() is True

    DummyDateTime.hour = 7
    assert bl.is_quiet_time() is True

    DummyDateTime.hour = 12
    assert bl.is_quiet_time() is False


def test_mark_and_load_printed(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(bl, "DB_FILE", str(db))
    bl.ensure_db()
    bl.mark_as_printed("abc")
    orders = bl.load_printed_orders()
    assert "abc" in orders


def test_mark_as_printed_deduplicates(tmp_path, monkeypatch):
    db = tmp_path / "test_dupes.db"
    monkeypatch.setattr(bl, "DB_FILE", str(db))
    bl.ensure_db()

    import datetime as dt

    class DummyDateTime(dt.datetime):
        ts = dt.datetime.fromisoformat("2023-01-01T00:00:00")

        @classmethod
        def now(cls, tz=None):
            return cls.ts

    monkeypatch.setattr(bl, "datetime", DummyDateTime)

    bl.mark_as_printed("xyz")
    first = bl.load_printed_orders()["xyz"]

    DummyDateTime.ts = dt.datetime.fromisoformat("2024-02-02T00:00:00")
    bl.mark_as_printed("xyz")
    second = bl.load_printed_orders()["xyz"]

    assert first == second


def test_queue_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "queue.db"
    monkeypatch.setattr(bl, "DB_FILE", str(db))
    bl.ensure_db()
    item = {
        "order_id": "1",
        "label_data": "xxx",
        "ext": "pdf",
        "last_order_data": {"a": 1},
    }
    bl.save_queue([item])
    loaded = bl.load_queue()
    assert len(loaded) == 1
    assert loaded[0]["order_id"] == item["order_id"]
    assert loaded[0]["label_data"] == item["label_data"]
    assert loaded[0]["ext"] == item["ext"]
    assert loaded[0]["last_order_data"] == item["last_order_data"]
