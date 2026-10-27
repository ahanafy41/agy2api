"""Read Antigravity conversation SQLite files and extract model responses."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from proto_decoder import ProtoField, decode_message, find_fields, first_text, raw_text, walk_text


USER_STEP_TYPE = 14
MODEL_STEP_TYPE = 15
TOOL_STEP_TYPES = {8, 9}
ERROR_STEP_TYPE = 17
DONE_STATUS = 3

# Keywords that mark an agy error step (type 17) as an upstream/transport
# failure worth surfacing verbatim to the caller.
_ERROR_KEYWORDS = (
    "model unreachable",
    "forcibly closed",
    "terminated due to error",
    "retryable error",
    "failed to get load code assist",
    "stream reading error",
)


@dataclass(frozen=True)
class AgResponse:
    answer: str
    reasoning: str = ""
    tool_summaries: tuple[str, ...] = ()
    db_path: str = ""
    truncated: bool = False
    usage: dict[str, int] = field(default_factory=dict)


class UpstreamError(ValueError):
    """agy/Google transport-side failure worth retrying (rate-limit, transient
    backend drop, model unreachable). Distinct from parse errors and from
    content-safety rejections, which are NOT retryable.

    Carried by read_response so agy_runner can decide retry-vs-surface without
    re-string-matching the message text."""


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Open SQLite in read-only mode to avoid write locks and journal files."""
    try:
        return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5.0)
    except Exception:
        return sqlite3.connect(path, timeout=5.0)


def _fetch_tool_summaries(conn: sqlite3.Connection) -> list[str]:
    """Extract tool execution summaries only for completed tool steps."""
    tool_rows = conn.execute(
        "SELECT step_payload FROM steps WHERE step_type IN (8, 9) AND status = ? ORDER BY idx",
        (DONE_STATUS,),
    ).fetchall()
    summaries: list[str] = []
    for (payload,) in tool_rows:
        if isinstance(payload, bytes):
            fields = decode_message(payload)
            summaries.extend(_pick_tool_texts(fields))
    return summaries


def _extract_error_from_payloads(payloads: list[bytes]) -> str:
    """Pull a concise upstream-error message out of agy error steps (type 17)."""
    found: list[str] = []
    for payload in payloads:
        if not isinstance(payload, bytes):
            continue
        for text in walk_text(decode_message(payload)):
            normalized = " ".join(text.split())
            if len(normalized) < 10:
                continue
            low = normalized.lower()
            if any(kw in low for kw in _ERROR_KEYWORDS):
                snippet = normalized[:200]
                if snippet not in found:
                    found.append(snippet)
    return " | ".join(found[:2])


def read_response(db_path: str | Path) -> AgResponse:
    path = Path(db_path)
    conn = _connect_ro(path)
    try:
        # Scan completed model rows (newest first) for one that actually carries
        # an answer. agy can emit several type-15 rows — reasoning-only, empty
        # terminal markers, etc. — so picking just the latest row with an answer.
        # Querying WHERE step_type = 15 avoids loading large user prompt blobs or
        # unnecessary intermediate step payloads into memory.
        model_rows = conn.execute(
            "SELECT idx, status, step_payload FROM steps WHERE step_type = ? ORDER BY idx DESC",
            (MODEL_STEP_TYPE,),
        ).fetchall()

        reasoning_only = ""
        for _, status, payload in model_rows:
            if status == DONE_STATUS and isinstance(payload, bytes):
                answer, reasoning = _extract_model_response(payload)
                if answer:
                    tool_summaries = _fetch_tool_summaries(conn)
                    return AgResponse(
                        answer=answer,
                        reasoning=reasoning,
                        tool_summaries=tuple(tool_summaries),
                        db_path=str(path),
                    )
                if reasoning and not reasoning_only:
                    reasoning_only = reasoning

        # No answer anywhere. If agy logged an upstream/transport error (type 17),
        # surface it verbatim — this is usually a network/proxy drop to Google, not
        # a parsing problem.
        error_rows = conn.execute(
            "SELECT step_payload FROM steps WHERE step_type = ? ORDER BY idx",
            (ERROR_STEP_TYPE,),
        ).fetchall()
        error = _extract_error_from_payloads([p for (p,) in error_rows])
        if error:
            raise UpstreamError(error)

        # Degraded read: agy killed mid-generation may leave a partial (non-DONE)
        # row whose text is still better than a blank response.
        for _, status, payload in model_rows:
            if status != DONE_STATUS and isinstance(payload, bytes):
                answer, reasoning = _extract_model_response(payload)
                if answer or reasoning:
                    tool_summaries = _fetch_tool_summaries(conn)
                    return AgResponse(
                        answer=answer,
                        reasoning=reasoning,
                        tool_summaries=tuple(tool_summaries),
                        db_path=str(path),
                        truncated=True,
                    )

        # Last resort: reasoning was produced but no answer and no explicit error.
        if reasoning_only:
            tool_summaries = _fetch_tool_summaries(conn)
            return AgResponse(
                answer="",
                reasoning=reasoning_only,
                tool_summaries=tuple(tool_summaries),
                db_path=str(path),
                truncated=True,
            )

        raise ValueError(f"no model response found in {path}")
    finally:
        conn.close()



def _extract_model_response(payload: bytes) -> tuple[str, str]:
    fields = decode_message(payload)
    response_fields = [
        field.value
        for field in find_fields(fields, 20)
        if isinstance(field.value, list)
    ]
    if not response_fields:
        return "", ""

    response = response_fields[-1]
    answer = raw_text(response, 1)
    reasoning = raw_text(response, 3)
    return answer, reasoning


def _extract_error(rows: list[tuple[int, int, int, bytes]]) -> str:
    """Pull a concise upstream-error message out of agy error steps (type 17)."""
    found: list[str] = []
    for _, step_type, _status, payload in rows:
        if step_type != ERROR_STEP_TYPE or not isinstance(payload, bytes):
            continue
        for text in walk_text(decode_message(payload)):
            normalized = " ".join(text.split())
            if len(normalized) < 10:
                continue
            low = normalized.lower()
            if any(kw in low for kw in _ERROR_KEYWORDS):
                snippet = normalized[:200]
                if snippet not in found:
                    found.append(snippet)
    return " | ".join(found[:2])


def _extract_tool_summaries(rows: list[tuple[int, int, int, bytes]]) -> list[str]:
    summaries: list[str] = []
    for _, step_type, status, payload in rows:
        if step_type not in TOOL_STEP_TYPES or status != DONE_STATUS or not isinstance(payload, bytes):
            continue
        fields = decode_message(payload)
        summaries.extend(_pick_tool_texts(fields))
    return summaries


def _pick_tool_texts(fields: list[ProtoField]) -> list[str]:
    seen: set[str] = set()
    picked: list[str] = []
    for text in walk_text(fields):
        clean = text.strip()
        if not clean or clean in seen:
            continue
        if len(clean) > 240:
            clean = clean[:237] + "..."
        if any(marker in clean.lower() for marker in ("view_file", "list_dir", "tool", "file://")):
            picked.append(clean)
            seen.add(clean)
    return picked
