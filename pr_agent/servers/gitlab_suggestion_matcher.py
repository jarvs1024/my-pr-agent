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
        parent_lines, current_lines, opcodes = file_cache[file_path]
        patch_lines = _normalize_patch_lines(patch)
        # Path 1: replacement match. The patch content exists in the
        # post-commit file at a span that overlaps a changed range and is
        # not byte-equal to the parent's corresponding region. Handles
        # ``-``/``+`` hunks.
        current_spans = _find_spans(current_lines, patch_lines)
        applied = any(
            any(_ranges_overlap(span, changed_range) for changed_range in changed_ranges)
            and not _span_is_unchanged(span, opcodes)
            for span in current_spans
        )
        # Path 2: deletion match. The patch content was removed from the
        # file in this commit (e.g. the suggestion was "delete this
        # redundant update"). ``_find_spans`` over current_lines finds
        # nothing because the patch is gone, so the replacement path
        # alone misses pure-deletion Apply commits and leaves the
        # suggestion in state ``open`` even though the user clicked
        # Apply. Fall back to searching parent_lines: if the patch was
        # there and the SequenceMatcher opcodes show the parent's patch
        # region was deleted or shrunk, AND a changed_range covers the
        # approximate current position of that region, treat as applied.
        if not applied:
            applied = any(
                _patch_region_deleted(
                    parent_span, opcodes, changed_ranges, parent_lines, current_lines
                )
                for parent_span in _find_spans(parent_lines, patch_lines)
            )
        if applied:
            candidates.append(suggestion_id)
    return candidates


def _patch_region_deleted(
    parent_span: tuple[int, int],
    opcodes: list[tuple[str, int, int, int, int]],
    changed_ranges: list[tuple[int, int]],
    parent_lines: list[str],
    current_lines: list[str],
) -> bool:
    """Return True when the suggestion's patch was deleted (or shrunk) by
    the commit whose opcodes we have, and a changed_range from the
    commit covers the approximate current position.

    ``parent_span`` is 1-based (start, end) inclusive of end, matching
    :func:`_find_spans`. ``opcodes`` are 0-based SequenceMatcher
    opcodes on ``parent_lines`` / ``current_lines``.
    """
    pi1_1based, pi2_1based = parent_span
    # Convert to 0-based half-open [start, end).
    p_start = pi1_1based - 1
    p_end = pi2_1based  # _find_spans returns inclusive end; convert.
    for tag, i1, i2, j1, j2 in opcodes:
        if i2 <= p_start:
            continue
        if i1 >= p_end:
            break
        # Overlap test in 0-based half-open intervals.
        if max(i1, p_start) >= min(i2, p_end):
            continue
        if tag == "delete":
            # The patch was removed entirely; current position is j1
            # (0-based) which corresponds to 1-based line (j1 + 1).
            current_pos = j1 + 1
            return any(
                _ranges_overlap((current_pos, current_pos + 1), cr)
                or _ranges_overlap((max(1, current_pos - 1), current_pos + 1), cr)
                for cr in changed_ranges
            )
        if tag == "replace":
            # Content shrunk (deletion happened as part of the replace).
            parent_size = i2 - i1
            current_size = j2 - j1
            if current_size < parent_size:
                current_pos = j1 + 1
                return any(
                    _ranges_overlap((current_pos, current_pos + 1), cr)
                    for cr in changed_ranges
                )
            return False
    return False


def target_region_changed(
    posted_content: str,
    current_content: str,
    line: int,
    line_end: int | None,
    context_lines: int = 1,
) -> bool:
    """Return True when the suggestion's target region has been touched by
    the user. False when the target code is byte-identical in the current
    file (modulo line drift / unrelated deletions elsewhere).

    Three-stage check:

      1. **Context window** (legacy): ``difflib.SequenceMatcher`` on the
         whole file; if the [start-context, end+context] window is fully
         covered by a single ``equal`` opcode, treat as unchanged. This
         preserves the original "user edited anywhere in the +/-1 line
         neighborhood of the suggestion" signal.
      2. **Length sanity**: if the current file is shorter than the
         target's last line, the target region was deleted — unchanged.
      3. **Verbatim drift check**: even when the context window shows a
         change (e.g. user added a header that shifted every line down),
         the target content may still appear verbatim elsewhere in the
         current file. If the longest contiguous equal slice of
         ``target_posted`` in ``current_lines`` covers the whole target,
         the user only shifted lines — the suggestion's target code is
         intact, so we still return False.
    """
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
    # equal_start <= old_start AND equal_end >= old_end
    # i.e. an equal opcode CONTAINS the [old_start, old_end) window.
    # The previous shape (``equal_end <= old_end``) required the equal
    # opcode to fit *inside* the window, which false-positives as soon
    # as an edit lives in the +/-context area but the target itself is
    # untouched (the equal opcode straddles the window boundary).
    primary_unchanged = any(
        tag == "equal" and equal_start <= old_start and equal_end >= old_end
        for tag, equal_start, equal_end, _, _ in opcodes
    )
    if not primary_unchanged:
        # The +/-1 context window has a real edit (replace / insert /
        # delete) — accept /adopt.
        return True
    # Stage 2: length sanity. If the current file is too short to hold
    # the target's last line, the target was deleted.
    if len(current_lines) < end_line:
        return False
    # Stage 3: verbatim drift check. The context window was unchanged
    # in the LCS sense, but verify that the target itself is still
    # present verbatim — if yes, the user only shifted lines around
    # without touching the suggestion's target code.
    target_posted = posted_lines[start_line - 1:end_line]
    if not target_posted:
        return False
    matcher = difflib.SequenceMatcher(a=target_posted, b=current_lines, autojunk=False)
    longest = 0
    for tag, i1, i2, _, _ in matcher.get_opcodes():
        if tag == "equal":
            longest = max(longest, i2 - i1)
    if longest >= len(target_posted):
        # Target content fully present verbatim somewhere in current;
        # user only added/removed unrelated lines.
        return False
    if longest == 0:
        # Target content entirely missing from current — user replaced
        # or deleted it without keeping the suggestion's code anywhere.
        return False
    return True
