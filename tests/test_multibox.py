"""Multi-box: migration backfill, box stamping, export→ingest round-trip, idempotency."""
import gzip
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_dashboard.db import (
    init_db, connect, overview_totals, model_breakdown, boxes, local_box,
)
from token_dashboard.export import export_delta, WATERMARK_KEY
from token_dashboard.ingest import ingest_file, sweep_inbox
from token_dashboard.scanner import scan_dir


def _jsonl_record(uuid, session="s1", ts="2026-08-13T12:00:00.000Z", typ="assistant",
                  model="claude-fable-5", msg_id=None, in_tok=100, out_tok=50):
    rec = {
        "uuid": uuid, "parentUuid": None, "sessionId": session, "type": typ,
        "timestamp": ts, "cwd": "/tmp/proj",
        "message": {"model": model, "id": msg_id,
                    "usage": {"input_tokens": in_tok, "output_tokens": out_tok}},
    }
    if typ == "user":
        rec["message"] = {"content": "hello world"}
    return rec


def _write_jsonl(root, slug, name, records):
    d = Path(root) / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return p


class MultiboxBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects, exist_ok=True)


class BoxMigrationTests(MultiboxBase):
    def _legacy_db(self):
        """Create a pre-multibox DB (no box column) with one row."""
        with sqlite3.connect(self.db) as c:
            c.execute("""CREATE TABLE messages (
                uuid TEXT PRIMARY KEY, parent_uuid TEXT, session_id TEXT NOT NULL,
                project_slug TEXT NOT NULL, cwd TEXT, git_branch TEXT, cc_version TEXT,
                entrypoint TEXT, type TEXT NOT NULL, is_sidechain INTEGER NOT NULL DEFAULT 0,
                agent_id TEXT, timestamp TEXT NOT NULL, model TEXT, stop_reason TEXT,
                prompt_id TEXT, message_id TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_create_5m_tokens INTEGER NOT NULL DEFAULT 0,
                cache_create_1h_tokens INTEGER NOT NULL DEFAULT 0,
                prompt_text TEXT, prompt_chars INTEGER, tool_calls_json TEXT)""")
            c.execute("""CREATE TABLE tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, message_uuid TEXT NOT NULL,
                session_id TEXT NOT NULL, project_slug TEXT NOT NULL, tool_name TEXT NOT NULL,
                target TEXT, result_tokens INTEGER, is_error INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL)""")
            c.execute("""INSERT INTO messages (uuid, session_id, project_slug, type, timestamp,
                         input_tokens, output_tokens)
                         VALUES ('u1','s1','p','assistant','2026-08-01T00:00:00Z',10,5)""")
            c.execute("""INSERT INTO tool_calls (message_uuid, session_id, project_slug,
                         tool_name, timestamp)
                         VALUES ('u1','s1','p','Read','2026-08-01T00:00:00Z')""")

    def test_migration_backfills_local_box(self):
        self._legacy_db()
        with mock.patch.dict(os.environ, {"TOKEN_DASHBOARD_BOX": "BMF"}):
            init_db(self.db)
        with connect(self.db) as c:
            self.assertEqual(c.execute("SELECT box FROM messages").fetchone()["box"], "BMF")
            self.assertEqual(c.execute("SELECT box FROM tool_calls").fetchone()["box"], "BMF")
        # row survived, nothing wiped
        self.assertEqual(overview_totals(self.db)["input_tokens"], 10)

    def test_migration_idempotent(self):
        self._legacy_db()
        with mock.patch.dict(os.environ, {"TOKEN_DASHBOARD_BOX": "BMF"}):
            init_db(self.db)
            init_db(self.db)
        self.assertEqual(boxes(self.db), [{"box": "BMF", "rows": 1}])


class BoxStampAndFilterTests(MultiboxBase):
    def test_scan_stamps_box_and_queries_filter(self):
        init_db(self.db)
        _write_jsonl(self.projects, "proj-a", "a.jsonl",
                     [_jsonl_record("m1", ts="2026-08-13T12:00:00.000Z")])
        scan_dir(self.projects, self.db, box="BMF")
        _write_jsonl(self.projects, "proj-a", "b.jsonl",
                     [_jsonl_record("m2", session="s2", ts="2026-08-13T13:00:00.000Z")])
        scan_dir(self.projects, self.db, box="VPS")

        self.assertEqual(overview_totals(self.db)["input_tokens"], 200)
        self.assertEqual(overview_totals(self.db, box="BMF")["input_tokens"], 100)
        self.assertEqual(overview_totals(self.db, box="VPS")["input_tokens"], 100)
        self.assertEqual(model_breakdown(self.db, box="BMF")[0]["turns"], 1)
        self.assertEqual({b["box"] for b in boxes(self.db)}, {"BMF", "VPS"})

    def test_local_box_env_override(self):
        with mock.patch.dict(os.environ, {"TOKEN_DASHBOARD_BOX": "LMF"}):
            self.assertEqual(local_box(), "LMF")


class ExportIngestTests(MultiboxBase):
    def _source_db(self, n=3):
        init_db(self.db)
        recs = [_jsonl_record(f"m{i}", session=f"s{i}",
                              ts=f"2026-08-13T1{i}:00:00.000Z") for i in range(n)]
        _write_jsonl(self.projects, "proj-a", "a.jsonl", recs)
        scan_dir(self.projects, self.db, box="BMF")

    def test_round_trip_reproduces_totals(self):
        self._source_db()
        out = os.path.join(self.tmp, "delta.json.gz")
        n = export_delta(self.db, out, box="BMF")
        self.assertEqual(n["messages"], 3)

        dest = os.path.join(self.tmp, "dest.db")
        init_db(dest)
        ingest_file(dest, out)
        self.assertEqual(overview_totals(dest), overview_totals(self.db))
        self.assertEqual(boxes(dest), [{"box": "BMF", "rows": 3}])

    def test_double_ingest_no_drift(self):
        self._source_db()
        out = os.path.join(self.tmp, "delta.json.gz")
        export_delta(self.db, out, box="BMF")
        dest = os.path.join(self.tmp, "dest.db")
        init_db(dest)
        ingest_file(dest, out)
        before = overview_totals(dest)
        ingest_file(dest, out)
        self.assertEqual(overview_totals(dest), before)
        with connect(dest) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"],
                             0)

    def test_watermark_advances_and_second_export_is_delta(self):
        # days apart so only the newest row sits inside the 24h overlap window
        init_db(self.db)
        recs = [_jsonl_record(f"m{i}", session=f"s{i}",
                              ts=f"2026-08-{d:02d}T00:00:00.000Z")
                for i, d in enumerate((1, 5, 10))]
        _write_jsonl(self.projects, "proj-a", "a.jsonl", recs)
        scan_dir(self.projects, self.db, box="BMF")
        out1 = os.path.join(self.tmp, "d1.json.gz")
        export_delta(self.db, out1, box="BMF")
        with connect(self.db) as c:
            wm = c.execute("SELECT v FROM plan WHERE k=?", (WATERMARK_KEY,)).fetchone()["v"]
        self.assertEqual(wm, "2026-08-10T00:00:00.000Z")

        _write_jsonl(self.projects, "proj-a", "b.jsonl",
                     [_jsonl_record("m9", session="s9", ts="2026-08-15T12:00:00.000Z")])
        scan_dir(self.projects, self.db, box="BMF")
        out2 = os.path.join(self.tmp, "d2.json.gz")
        n = export_delta(self.db, out2, box="BMF")
        # m9 (new) + m2 (inside the overlap window behind the watermark)
        self.assertEqual(n["messages"], 2)

    def test_export_only_local_box_rows(self):
        """Ingested foreign rows must never be re-exported (no echo loops)."""
        self._source_db()
        _write_jsonl(self.projects, "proj-a", "z.jsonl",
                     [_jsonl_record("v1", session="sv", ts="2026-08-13T14:00:00.000Z")])
        scan_dir(self.projects, self.db, box="VPS")
        out = os.path.join(self.tmp, "delta.json.gz")
        n = export_delta(self.db, out, box="BMF")
        self.assertEqual(n["messages"], 3)
        with gzip.open(out, "rt", encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual({m["box"] for m in payload["messages"]}, {"BMF"})

    def test_sweep_inbox_deletes_on_success_keeps_bad(self):
        self._source_db()
        inbox = Path(self.tmp) / "inbox"
        inbox.mkdir()
        export_delta(self.db, inbox / "td-BMF-1.json.gz", box="BMF")
        (inbox / "td-junk.json.gz").write_bytes(gzip.compress(b"not json"))

        dest = os.path.join(self.tmp, "dest.db")
        init_db(dest)
        n = sweep_inbox(dest, inbox)
        self.assertEqual(n["files"], 1)
        self.assertEqual(n["errors"], 1)
        self.assertEqual(overview_totals(dest)["input_tokens"], 300)
        left = sorted(p.name for p in inbox.iterdir())
        self.assertEqual(left, ["td-junk.json.gz.bad"])


if __name__ == "__main__":
    unittest.main()
