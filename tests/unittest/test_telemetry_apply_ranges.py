from pr_agent.config_loader import get_settings  # noqa: F401
from pr_agent.telemetry import events, models
from pr_agent.telemetry.store import TelemetryStore


def _record(store: TelemetryStore, suggestion_id: str, line: int) -> None:
    store.record_suggestion(models.Suggestion(
        suggestion_id=suggestion_id,
        mr_id=92,
        project_id=34,
        file="services/inventory.py",
        line=line,
        label="bug",
        importance=7,
        one_sentence_summary=suggestion_id,
    ))


def test_mark_lines_applied_only_updates_suggestions_in_changed_ranges(tmp_path):
    store = TelemetryStore("sqlite", sqlite_path=str(tmp_path / "telemetry.db"))
    _record(store, "sug-line-10", 10)
    _record(store, "sug-line-20", 20)
    _record(store, "sug-line-30", 30)

    updated = store.mark_lines_applied(
        mr_id=92,
        project_id=34,
        file="services/inventory.py",
        line_ranges=[(9, 11), (29, 31)],
        applied_at="2026-07-23T10:30:00+00:00",
    )

    states = {row["suggestion_id"]: row["state"] for row in store.list_suggestions(92, 34)}
    assert set(updated) == {"sug-line-10", "sug-line-30"}
    assert states == {
        "sug-line-10": "applied",
        "sug-line-20": "open",
        "sug-line-30": "applied",
    }


def test_mark_lines_applied_with_empty_ranges_is_noop(tmp_path):
    store = TelemetryStore("sqlite", sqlite_path=str(tmp_path / "telemetry.db"))
    _record(store, "sug-line-10", 10)

    updated = store.mark_lines_applied(
        mr_id=92,
        project_id=34,
        file="services/inventory.py",
        line_ranges=[],
        applied_at="2026-07-23T10:30:00+00:00",
    )

    assert updated == []
    assert store.list_suggestions(92, 34)[0]["state"] == "open"


def test_mark_suggestions_applied_passes_line_ranges_to_store(monkeypatch):
    class FakeStore:
        def __init__(self):
            self.calls = []

        def mark_lines_applied(self, **kwargs):
            self.calls.append(kwargs)
            return []

    store = FakeStore()
    monkeypatch.setattr(events, "get_default_store", lambda: store)

    events.mark_suggestions_applied(
        mr_id=92,
        project_id=34,
        file="services/inventory.py",
        line_ranges=[(20, 20)],
        applied_at="2026-07-23T10:30:00+00:00",
    )

    assert store.calls == [{
        "mr_id": 92,
        "project_id": 34,
        "file": "services/inventory.py",
        "line_ranges": [(20, 20)],
        "applied_at": "2026-07-23T10:30:00+00:00",
    }]
