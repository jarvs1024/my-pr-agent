from pr_agent.config_loader import get_settings  # noqa: F401
from pr_agent.telemetry import events, models
from pr_agent.telemetry.store import TelemetryStore


def _record(store: TelemetryStore, suggestion_id: str, line: int, *, state: str = "open", mr_id: int = 92) -> None:
    store.record_suggestion(models.Suggestion(
        suggestion_id=suggestion_id,
        mr_id=mr_id,
        project_id=34,
        file="services/inventory.py",
        line=line,
        label="bug",
        importance=7,
        one_sentence_summary=suggestion_id,
        state=state,
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


def test_mark_suggestion_ids_applied_updates_only_exact_open_ids(tmp_path):
    store = TelemetryStore("sqlite", sqlite_path=str(tmp_path / "telemetry.db"))
    _record(store, "sug-lookup", 17)
    _record(store, "sug-average", 24)

    updated = store.mark_suggestion_ids_applied(
        mr_id=92,
        project_id=34,
        suggestion_ids=["sug-lookup"],
        applied_at="2026-07-25T08:00:00+00:00",
    )

    states = {row["suggestion_id"]: row["state"] for row in store.list_suggestions(92, 34)}
    assert updated == ["sug-lookup"]
    assert states == {"sug-lookup": "applied", "sug-average": "open"}


def test_mark_suggestion_ids_applied_ignores_invalid_or_resolved_ids(tmp_path):
    store = TelemetryStore("sqlite", sqlite_path=str(tmp_path / "telemetry.db"))
    _record(store, "sug-open", 10)
    _record(store, "sug-dismissed", 20, state="dismissed")
    _record(store, "sug-other-mr", 30, mr_id=93)

    updated = store.mark_suggestion_ids_applied(
        mr_id=92,
        project_id=34,
        suggestion_ids=["sug-dismissed", "sug-other-mr", "missing", "sug-open", "sug-open"],
        applied_at="2026-07-25T08:00:00+00:00",
    )

    assert updated == ["sug-open"]
    states = {row["suggestion_id"]: row["state"] for row in store.list_suggestions(92, 34)}
    assert states == {"sug-open": "applied", "sug-dismissed": "dismissed"}


def test_mark_suggestion_ids_applied_records_one_action_per_updated_id(monkeypatch):
    class FakeStore:
        def __init__(self):
            self.actions = []

        def mark_suggestion_ids_applied(self, **kwargs):
            assert kwargs["suggestion_ids"] == ["sug-1", "sug-2"]
            return ["sug-1", "sug-2"]

        def record_action(self, action):
            self.actions.append(action)

    store = FakeStore()
    monkeypatch.setattr(events, "get_default_store", lambda: store)

    updated = events.mark_suggestion_ids_applied(
        mr_id=92,
        project_id=34,
        suggestion_ids=["sug-1", "sug-2"],
        actor="alice",
        apply_event_sha="abc123",
        applied_at="2026-07-25T08:00:00+00:00",
    )

    assert updated == ["sug-1", "sug-2"]
    assert [(action.action, action.suggestion_id, action.note) for action in store.actions] == [
        ("applied", "sug-1", "commit abc123"),
        ("applied", "sug-2", "commit abc123"),
    ]
