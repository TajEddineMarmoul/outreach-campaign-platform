"""Measure indexed dispatch selection against a large, isolated on-disk queue.

No production connection or Gmail calls. The temporary SQLite database is
deleted afterwards. This measures queue selection, not Gmail throughput or
PostgreSQL concurrency (those have separate integration checks).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

from src.platform.models import Base, SenderDispatch
from src.platform.shared_dispatch import maintain, release_slot, reserve_slot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaigns", type=int, default=1_000_000)
    parser.add_argument("--selections", type=int, default=100)
    args = parser.parse_args()
    if args.campaigns < 1 or args.selections < 1:
        parser.error("Counts must be positive")
    temporary = tempfile.NamedTemporaryFile(prefix="outreach-dispatch-benchmark-", suffix=".db", delete=False)
    database = Path(temporary.name).resolve()
    temporary.close()
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    now = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
    started = time.perf_counter()
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA synchronous=NORMAL")
            connection.exec_driver_sql("PRAGMA cache_size=-32768")
            connection.exec_driver_sql("PRAGMA temp_store=MEMORY")
            stamp = now.replace(tzinfo=None).isoformat(" ", timespec="microseconds")
            end = (now + timedelta(hours=2)).replace(tzinfo=None).isoformat(" ", timespec="microseconds")
            connection.execute(text("INSERT INTO users (id, created_at, updated_at) VALUES ('benchmark', :stamp, :stamp)"), {"stamp": stamp})
            connection.execute(text("INSERT INTO sender_groups (id, user_id, name, created_at, updated_at) VALUES (1, 'benchmark', 'Benchmark', :stamp, :stamp)"), {"stamp": stamp})
            connection.execute(text("""INSERT INTO senders
                (id, user_id, group_id, email, display_name, daily_cap, is_default, status,
                 encrypted_oauth_credentials, scopes, gmail_tracking_status, gmail_push_pending, created_at, updated_at)
                VALUES (1, 'benchmark', 1, 'benchmark@example.test', '', 1000, 0, 'connected',
                        'test-only', '[]', 'needs_reconnect', 0, :stamp, :stamp)"""), {"stamp": stamp})
            connection.execute(text("""WITH RECURSIVE seq(id) AS
                (SELECT 1 UNION ALL SELECT id + 1 FROM seq WHERE id < :count)
                INSERT INTO campaigns
                (id, user_id, selected_sender_group_id, name, subject_template, body_template,
                 fallback_body_template, attachment_path, status, timezone, send_settings,
                 attachment_metadata, engagement_tracking_enabled, shared_dispatch, created_at, updated_at)
                SELECT id, 'benchmark', 1, 'Benchmark', 'Hi', 'Hello', '', '', 'autopilot', 'UTC',
                       '{}', '{}', 0, 1, :stamp, :stamp FROM seq"""), {"count": args.campaigns, "stamp": stamp})
            connection.execute(text("""INSERT INTO sender_campaign_lanes
                (sender_id, campaign_id, active, selectable, rate, finish_tag,
                 window_start, window_end, transition_at, calendar)
                SELECT 1, id, 0, 0, 1.0 / 7200, 0, :stamp, :end, :stamp, :calendar
                FROM campaigns"""), {"stamp": stamp, "end": end, "calendar": json.dumps({
                    "timezone": "UTC", "mode": "autopilot", "pacing": "spread_evenly", "senders": 1,
                    "not_before": None, "fixed_rate": 1/300,
                    "days": {"monday": {"cap": 1, "start": "10:00", "end": "12:00"}},
                })})
        with Session(engine) as session:
            session.add(SenderDispatch(sender_id=1, user_id="benchmark", rate=0,
                active_lanes=0, virtual_time=0, credit=1, clock_at=now, idle_since=now))
            session.commit()
        setup = time.perf_counter() - started
        statements = []
        event.listen(engine, "before_cursor_execute", lambda *_: statements.append(1))
        with Session(engine) as session:
            start = time.perf_counter()
            opening = maintain(session, limit=1, now=now)
            opening_ms = (time.perf_counter() - start) * 1000
            opening_statements = len(statements)
            assert opening["boundaries"] == 1 and opening["lanes"] == args.campaigns
        timings, counts = [], []
        with Session(engine, expire_on_commit=False) as session:
            for _ in range(args.selections):
                before = len(statements)
                start = time.perf_counter()
                reservation = reserve_slot(session, now=now)
                timings.append((time.perf_counter() - start) * 1000)
                counts.append(len(statements) - before)
                if reservation is None:
                    raise AssertionError("Expected a due indexed selection")
                release_slot(session, reservation[0], reservation[2], now=now)
                session.commit()
                now = session.get(SenderDispatch, 1).next_at
        with engine.connect() as connection:
            plan = [row[3] for row in connection.exec_driver_sql("""EXPLAIN QUERY PLAN
                SELECT l.campaign_id FROM sender_campaign_lanes AS l
                WHERE l.sender_id = 1 AND l.active = 1 AND l.selectable = 1
                  AND l.window_start <= '2026-10-26 10:00:00'
                  AND l.window_end > '2026-10-26 10:00:00'
                ORDER BY l.finish_tag, l.window_end, l.campaign_id LIMIT 1""")]
        if not any("ix_sender_lanes_fair" in row for row in plan):
            raise AssertionError(f"Fair queue index was not used: {plan}")
        print(json.dumps({"campaigns": args.campaigns, "queue_entries": args.campaigns,
            "selections": args.selections, "setup_seconds": round(setup, 2),
            "median_selection_ms": round(statistics.median(timings), 3),
            "p95_selection_ms": round(sorted(timings)[int((len(timings)-1)*0.95)], 3),
            "opening_boundary_ms": round(opening_ms, 3), "opening_sql_statements": opening_statements,
            "sql_statements_per_selection": sorted(set(counts)), "query_plan": plan}, indent=2))
    finally:
        engine.dispose()
        # Only the explicitly created temporary files are removed.
        for path in [database, Path(str(database) + "-wal"), Path(str(database) + "-shm")]:
            if path.exists():
                path.unlink()


if __name__ == "__main__":
    main()
