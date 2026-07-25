from pr_agent.servers.gitlab_suggestion_matcher import (
    extract_suggestion_patch,
    find_applied_suggestion_candidates,
    target_region_changed,
)


def _note(code: str) -> dict:
    return {
        "body": (
            "**Suggestion:** fix\n"
            "```suggestion:-0+1\n"
            f"{code}\n"
            "```"
        )
    }


def _suggestion(suggestion_id: str, discussion_id: str, file: str = "service.py") -> dict:
    return {
        "suggestion_id": suggestion_id,
        "note_id": discussion_id,
        "file": file,
        "state": "open",
    }


def test_extract_suggestion_patch_normalizes_line_endings_and_trailing_spaces():
    body = "prefix\n```suggestion:-0+2\r\ndef run():  \r\n    return 1\r\n```\nsuffix"

    assert extract_suggestion_patch(body) == "def run():\n    return 1"


def test_extract_suggestion_patch_rejects_missing_or_empty_block():
    assert extract_suggestion_patch("plain comment") is None
    assert extract_suggestion_patch("```suggestion\n\n```") is None


def test_lookup_apply_does_not_match_adjacent_average():
    parent = """import sqlite3


def lookup_user(name):
    conn = sqlite3.connect("app.db")
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE name = '" + name + "'")
    return cur.fetchall()


def average(values):
    return sum(values) / len(values)
"""
    lookup_patch = """def lookup_user(name: str) -> list:
    conn = sqlite3.connect("app.db")
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE name = ?", (name,))
        return cur.fetchall()
    finally:
        conn.close()"""
    average_patch = """def average(values: list) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)"""
    current = """import sqlite3


def lookup_user(name: str) -> list:
    conn = sqlite3.connect("app.db")
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE name = ?", (name,))
        return cur.fetchall()
    finally:
        conn.close()


def average(values):
    return sum(values) / len(values)
"""

    candidates = find_applied_suggestion_candidates(
        open_suggestions=[
            _suggestion("sug-lookup", "discussion-lookup"),
            _suggestion("sug-average", "discussion-average"),
        ],
        notes_by_discussion={
            "discussion-lookup": _note(lookup_patch),
            "discussion-average": _note(average_patch),
        },
        parent_files={"service.py": parent},
        current_files={"service.py": current},
        changed_ranges_by_file={"service.py": [(4, 5), (7, 12)]},
    )

    assert candidates == ["sug-lookup"]


def test_patch_already_present_in_equal_region_is_not_a_candidate():
    patch = """def stable():
    return 1"""
    parent = patch + "\n\n\ndef changed():\n    return 1\n"
    current = patch + "\n\n\ndef changed():\n    return 2\n"

    candidates = find_applied_suggestion_candidates(
        open_suggestions=[_suggestion("sug-stable", "discussion-stable")],
        notes_by_discussion={"discussion-stable": _note(patch)},
        parent_files={"service.py": parent},
        current_files={"service.py": current},
        changed_ranges_by_file={"service.py": [(5, 6)]},
    )

    assert candidates == []


def test_changed_occurrence_matches_when_identical_line_exists_elsewhere():
    patch = "    logging.info(status)"
    parent = "def other(status):\n    logging.info(status)\n\n\ndef emit(status):\n    print(status)\n"
    current = "def other(status):\n    logging.info(status)\n\n\ndef emit(status):\n    logging.info(status)\n"

    candidates = find_applied_suggestion_candidates(
        open_suggestions=[_suggestion("sug-emit", "discussion-emit")],
        notes_by_discussion={"discussion-emit": _note(patch)},
        parent_files={"service.py": parent},
        current_files={"service.py": current},
        changed_ranges_by_file={"service.py": [(6, 6)]},
    )

    assert candidates == ["sug-emit"]


def test_missing_discussion_or_file_does_not_match():
    candidates = find_applied_suggestion_candidates(
        open_suggestions=[
            _suggestion("sug-missing-note", "missing-note"),
            _suggestion("sug-missing-file", "has-note", file="missing.py"),
        ],
        notes_by_discussion={"has-note": _note("return 1")},
        parent_files={"service.py": "return 0\n"},
        current_files={"service.py": "return 1\n"},
        changed_ranges_by_file={"service.py": [(1, 1)]},
    )

    assert candidates == []


def test_target_region_ignores_unrelated_function_change():
    posted = "import sqlite3\n\n\ndef run():\n    return 1\n"
    current = "import sqlite3\n\n\ndef run():\n    return 2\n"

    assert target_region_changed(posted, current, line=1, line_end=1, context_lines=1) is False


def test_target_region_detects_adjacent_import_insertion():
    posted = "import sqlite3\n\n\ndef run():\n    return 1\n"
    current = "import sqlite3\nimport logging\n\n\ndef run():\n    return 1\n"

    assert target_region_changed(posted, current, line=1, line_end=1, context_lines=1) is True


def test_target_region_detects_function_rewrite():
    posted = "def run():\n    return 1\n"
    current = "def run():\n    return 2\n"

    assert target_region_changed(posted, current, line=1, line_end=2) is True


def test_target_region_rejects_invalid_metadata():
    assert target_region_changed("x\n", "y\n", line=0, line_end=1) is False
    assert target_region_changed("x\n", "y\n", line=3, line_end=3) is False
