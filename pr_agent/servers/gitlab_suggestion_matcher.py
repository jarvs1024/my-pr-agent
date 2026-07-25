from __future__ import annotations

import difflib
import re


_SUGGESTION_BLOCK_RE = re.compile(
    r"```suggestion[^\n]*\n(.*?)\n```",
    flags=re.DOTALL | re.IGNORECASE,
)


def _normalize_line_endings(code: str) -> str:
    return (code or "").replace("\r\n", "\n").replace("\r", "\n")


def _normalize_patch_lines(code: str) -> list[str]:
    lines = [line.rstrip() for line in _normalize_line_endings(code).split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _normalize_file_lines(content: str) -> list[str]:
    lines = [line.rstrip() for line in _normalize_line_endings(content).split("\n")]
    if lines and not lines[-1]:
        lines.pop()
    return lines


def extract_suggestion_patch(body: str) -> str | None:
    match = _SUGGESTION_BLOCK_RE.search(body or "")
    if not match:
        return None
    lines = _normalize_patch_lines(match.group(1))
    return "\n".join(lines) if lines else None


def _find_spans(haystack: list[str], needle: list[str]) -> list[tuple[int, int]]:
    if not needle or len(needle) > len(haystack):
        return []
    return [
        (index + 1, index + len(needle))
        for index in range(len(haystack) - len(needle) + 1)
        if haystack[index:index + len(needle)] == needle
    ]


def _ranges_overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return first[0] <= second[1] and second[0] <= first[1]


def _span_is_unchanged(
    span: tuple[int, int],
    opcodes: list[tuple[str, int, int, int, int]],
) -> bool:
    current_start = span[0] - 1
    current_end = span[1]
    return any(
        tag == "equal" and new_start <= current_start and current_end <= new_end
        for tag, _, _, new_start, new_end in opcodes
    )


def find_applied_suggestion_candidates(
    open_suggestions: list[dict],
    notes_by_discussion: dict[str, dict],
    parent_files: dict[str, str],
    current_files: dict[str, str],
    changed_ranges_by_file: dict[str, list[tuple[int, int]]],
) -> list[str]:
    file_cache: dict[str, tuple[list[str], list[str], list[tuple[str, int, int, int, int]]]] = {}
    candidates = []
    for suggestion in open_suggestions or []:
        if suggestion.get("state") not in (None, "open"):
            continue
        suggestion_id = str(suggestion.get("suggestion_id") or "").strip()
        discussion_id = str(suggestion.get("note_id") or "").strip()
        file_path = str(suggestion.get("file") or "").strip()
        note = notes_by_discussion.get(discussion_id) or {}
        patch = extract_suggestion_patch(note.get("body") or "")
        if not suggestion_id or not discussion_id or not file_path or not patch:
            continue
        if file_path not in parent_files or file_path not in current_files:
            continue
        changed_ranges = changed_ranges_by_file.get(file_path) or []
        if not changed_ranges:
            continue
        if file_path not in file_cache:
            parent_lines = _normalize_file_lines(parent_files[file_path])
            current_lines = _normalize_file_lines(current_files[file_path])
            opcodes = difflib.SequenceMatcher(
                None,
                parent_lines,
                current_lines,
                autojunk=False,
            ).get_opcodes()
            file_cache[file_path] = parent_lines, current_lines, opcodes
        _, current_lines, opcodes = file_cache[file_path]
        patch_lines = _normalize_patch_lines(patch)
        applied = any(
            any(_ranges_overlap(span, changed_range) for changed_range in changed_ranges)
            and not _span_is_unchanged(span, opcodes)
            for span in _find_spans(current_lines, patch_lines)
        )
        if applied:
            candidates.append(suggestion_id)
    return candidates


def target_region_changed(
    posted_content: str,
    current_content: str,
    line: int,
    line_end: int | None,
    context_lines: int = 1,
) -> bool:
    posted_lines = _normalize_file_lines(posted_content)
    current_lines = _normalize_file_lines(current_content)
    start_line = int(line or 0)
    end_line = int(line_end or start_line)
    if start_line < 1 or end_line < start_line or start_line > len(posted_lines):
        return False
    context = max(0, int(context_lines or 0))
    old_start = max(0, start_line - 1 - context)
    old_end = min(len(posted_lines), end_line + context)
    if old_end <= old_start:
        return False
    opcodes = difflib.SequenceMatcher(
        None,
        posted_lines,
        current_lines,
        autojunk=False,
    ).get_opcodes()
    target_is_unchanged = any(
        tag == "equal" and equal_start <= old_start and old_end <= equal_end
        for tag, equal_start, equal_end, _, _ in opcodes
    )
    return not target_is_unchanged
