"""Ingest another box's export file(s) into this DB.

Replays the scanner's exact dedupe path — snapshot evict per (session_id,
message_id), INSERT OR REPLACE on uuid, delete-then-insert tool_calls per
message uuid — so ingesting the same file twice (or the export's 24h overlap
window) changes nothing.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Union

from .db import connect
from .scanner import INSERT_MSG, INSERT_TOOL, _evict_prior_snapshots


def ingest_file(db_path: Union[str, Path], path: Union[str, Path]) -> dict:
    path = Path(path)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict) or payload.get("format") != 1:
        raise ValueError(f"{path}: not a v1 token-dashboard export")

    messages = payload.get("messages") or []
    tool_calls = payload.get("tool_calls") or []

    with connect(db_path) as conn:
        for msg in messages:
            if msg.get("message_id"):
                _evict_prior_snapshots(conn, msg["session_id"], msg["message_id"], msg["uuid"])
            conn.execute(INSERT_MSG, msg)
            # Clear prior tool rows for every re-sent message so the overlap
            # window can't duplicate tool_calls (no natural unique key there).
            conn.execute("DELETE FROM tool_calls WHERE message_uuid=?", (msg["uuid"],))
        # Also clear by the tool rows' own parents — belt for the (window-edge)
        # case of a tool row arriving without its message in the same payload.
        seen = {m["uuid"] for m in messages}
        for uuid in {t["message_uuid"] for t in tool_calls} - seen:
            conn.execute("DELETE FROM tool_calls WHERE message_uuid=?", (uuid,))
        for t in tool_calls:
            conn.execute(INSERT_TOOL, t)
        conn.commit()

    return {"box": payload.get("box"), "messages": len(messages),
            "tool_calls": len(tool_calls)}


def sweep_inbox(db_path: Union[str, Path], inbox_dir: Union[str, Path]) -> dict:
    """Ingest every export in inbox_dir; delete each on success.

    A file that fails to parse is renamed to *.bad instead of retried forever.
    """
    inbox = Path(inbox_dir)
    totals = {"files": 0, "messages": 0, "tool_calls": 0, "errors": 0}
    if not inbox.is_dir():
        return totals
    for p in sorted(inbox.glob("*.json.gz")):
        try:
            n = ingest_file(db_path, p)
            p.unlink()
            totals["files"] += 1
            totals["messages"] += n["messages"]
            totals["tool_calls"] += n["tool_calls"]
        except Exception:
            totals["errors"] += 1
            try:
                p.rename(p.with_suffix(p.suffix + ".bad"))
            except OSError:
                pass
    return totals
