"""Tests for the new telemetry API endpoints for TestMate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pr_agent.telemetry import api as telemetry_api


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_AGENT_DATA_DIR", str(tmp_path))
    base = tmp_path / "weekly_reports" / "7"
    base.mkdir(parents=True)
    (base / "2026-W30.json").write_text(
        json.dumps({
            "schema_version": 1,
            "project_id": 7,
            "week_label": "2026-W30",
            "week_start": "2026-07-20T00:00:00+00:00",
            "week_end": "2026-07-26T23:59:59+00:00",
            "generated_at": "2026-07-26T12:00:00+00:00",
            "timezone": "UTC",
            "sections": {
                "telemetry": {"status": "ok", "data": {"mr_count": 1}},
                "master_merges": {"status": "failed", "error": "429"},
                "repo_scan": {"status": "ok", "data": {"x": 1}},
            },
        }),
        encoding="utf-8",
    )
    # Add a second, older artifact
    (base / "2026-W29.json").write_text(
        json.dumps({
            "schema_version": 1, "project_id": 7, "week_label": "2026-W29",
            "week_start": "2026-07-13T00:00:00+00:00", "week_end": "2026-07-19T23:59:59+00:00",
            "generated_at": "2026-07-19T12:00:00+00:00", "timezone": "UTC",
            "sections": {"telemetry": {"status": "ok"}},
        }),
        encoding="utf-8",
    )

    app = FastAPI()
    telemetry_api.install_routes(app)
    return TestClient(app)


def test_list_returns_newest_first(client):
    r = client.get("/api/v1/telemetry/weekly_reports/list?project_id=7")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert rows[0]["week_label"] == "2026-W30"
    assert rows[0]["has_failures"] is True
    assert rows[0]["section_status"]["master_merges"] == "failed"


def test_latest_returns_most_recent_artifact(client):
    r = client.get("/api/v1/telemetry/weekly_reports/latest?project_id=7")
    assert r.status_code == 200
    body = r.json()
    assert body["week_label"] == "2026-W30"
    assert body["project_id"] == 7


def test_latest_returns_404_when_missing(client):
    r = client.get("/api/v1/telemetry/weekly_reports/latest?project_id=999")
    assert r.status_code == 404


def test_list_returns_empty_when_no_artifacts(client, monkeypatch, tmp_path):
    empty = tmp_path / "empty-project"
    empty.mkdir()
    monkeypatch.setenv("PR_AGENT_DATA_DIR", str(tmp_path))
    r = client.get("/api/v1/telemetry/weekly_reports/list?project_id=999")
    assert r.status_code == 200
    assert r.json() == []
