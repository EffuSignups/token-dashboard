"""Delta export: this box's rows since the last export, as gzipped JSON.

The watermark (last exported timestamp) lives in the `plan` k/v table. Each
export re-sends a 24h overlap window behind the watermark; ingest replays the
scanner's own dedupe (INSERT OR REPLACE + snapshot evict + per-uuid tool_calls
delete), so overlap is idempotent by construction — a lost or duplicated file
never drifts the totals.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

from .db import connect, local_box

WATERMARK_KEY = "export_watermark"
OVERLAP_HOURS = 24
FORMAT_VERSION = 1

MSG_COLS = (
    "uuid, box, parent_uuid, session_id, project_slug, cwd, git_branch, cc_version, "
    "entrypoint, type, is_sidechain, agent_id, timestamp, model, stop_reason, prompt_id, "
    "message_id, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, "
    "cache_create_1h_tokens, prompt_text, prompt_chars, tool_calls_json"
)
TOOL_COLS = (
    "box, message_uuid, session_id, project_slug, tool_name, target, "
    "result_tokens, is_error, timestamp"
)


def _since_from_watermark(watermark: Optional[str]) -> Optional[str]:
    if not watermark:
        return None
    # Timestamps are ISO-8601 Z strings; string compare in SQL matches time
    # order, so we only parse here to subtract the overlap.
    try:
        dt = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
    except ValueError:
        return None
    since = dt - timedelta(hours=OVERLAP_HOURS)
    return since.isoformat().replace("+00:00", "Z")


def export_delta(db_path: Union[str, Path], out_path: Union[str, Path],
                 box: Optional[str] = None) -> dict:
    """Write this box's delta to out_path. Returns counts + the new watermark."""
    box = box or local_box()
    out_path = Path(out_path)
    with connect(db_path) as c:
        row = c.execute("SELECT v FROM plan WHERE k=?", (WATERMARK_KEY,)).fetchone()
        since = _since_from_watermark(row["v"] if row else None)

        where, args = "box = ?", [box]
        if since:
            where += " AND timestamp >= ?"
            args.append(since)

        messages = [dict(r) for r in c.execute(
            f"SELECT {MSG_COLS} FROM messages WHERE {where}", args)]
        tool_calls = [dict(r) for r in c.execute(
            f"SELECT {TOOL_COLS} FROM tool_calls WHERE {where}", args)]

        payload = {
            "format": FORMAT_VERSION,
            "box": box,
            "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "messages": messages,
            "tool_calls": tool_calls,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(out_path, "wt", encoding="utf-8") as f:
            json.dump(payload, f, default=str)

        new_watermark = max((m["timestamp"] for m in messages), default=None)
        if new_watermark:
            c.execute("INSERT OR REPLACE INTO plan (k, v) VALUES (?, ?)",
                      (WATERMARK_KEY, new_watermark))
            c.commit()

    return {"messages": len(messages), "tool_calls": len(tool_calls),
            "box": box, "watermark": new_watermark, "path": str(out_path)}
