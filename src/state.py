from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def is_complete(self, schedule_date: date, require_email: bool = False) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select status, email_sent_at from reports where schedule_date = ?",
                (schedule_date.isoformat(),),
            ).fetchone()
        if not row or row[0] != "success":
            return False
        return bool(row[1]) if require_email else True

    def mark_started(self, schedule_date: date) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                insert into reports(schedule_date, status, started_at)
                values(?, 'started', ?)
                on conflict(schedule_date) do update set
                    status='started',
                    started_at=excluded.started_at,
                    error_message=null
                """,
                (schedule_date.isoformat(), now),
            )

    def mark_success(self, schedule_date: date, markdown_path: Path, html_path: Path, email_sent: bool) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                insert into reports(schedule_date, status, generated_at, email_sent_at, markdown_path, html_path)
                values(?, 'success', ?, ?, ?, ?)
                on conflict(schedule_date) do update set
                    status='success',
                    generated_at=excluded.generated_at,
                    email_sent_at=case when ? then excluded.email_sent_at else reports.email_sent_at end,
                    markdown_path=excluded.markdown_path,
                    html_path=excluded.html_path,
                    error_message=null
                """,
                (
                    schedule_date.isoformat(),
                    now,
                    now if email_sent else None,
                    str(markdown_path),
                    str(html_path),
                    1 if email_sent else 0,
                ),
            )

    def mark_failed(self, schedule_date: date, error_message: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                insert into reports(schedule_date, status, started_at, error_message)
                values(?, 'failed', ?, ?)
                on conflict(schedule_date) do update set
                    status='failed',
                    error_message=excluded.error_message
                """,
                (schedule_date.isoformat(), now, error_message[:2000]),
            )

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists reports(
                    schedule_date text primary key,
                    status text not null,
                    started_at text,
                    generated_at text,
                    email_sent_at text,
                    markdown_path text,
                    html_path text,
                    error_message text
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


@contextmanager
def run_lock(lock_path: Path, stale_seconds: int = 7200) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        age = time.time() - lock_path.stat().st_mtime
        if age > stale_seconds:
            lock_path.unlink(missing_ok=True)
    fd: int | None = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        yield
    finally:
        if fd is not None:
            os.close(fd)
        lock_path.unlink(missing_ok=True)
