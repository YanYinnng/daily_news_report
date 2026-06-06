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

    def is_complete(self, schedule_date: date, require_email: bool = False, theme_id: str = "default") -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select status, email_sent_at from theme_reports where schedule_date = ? and theme_id = ?",
                (schedule_date.isoformat(), theme_id),
            ).fetchone()
        if not row or row[0] != "success":
            return False
        return bool(row[1]) if require_email else True

    def mark_started(self, schedule_date: date, theme_id: str = "default") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                insert into theme_reports(schedule_date, theme_id, status, started_at)
                values(?, ?, 'started', ?)
                on conflict(schedule_date, theme_id) do update set
                    status='started',
                    started_at=excluded.started_at,
                    error_message=null
                """,
                (schedule_date.isoformat(), theme_id, now),
            )

    def mark_success(
        self,
        schedule_date: date,
        markdown_path: Path,
        html_path: Path,
        email_sent: bool,
        theme_id: str = "default",
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                insert into theme_reports(schedule_date, theme_id, status, generated_at, email_sent_at, markdown_path, html_path)
                values(?, ?, 'success', ?, ?, ?, ?)
                on conflict(schedule_date, theme_id) do update set
                    status='success',
                    generated_at=excluded.generated_at,
                    email_sent_at=case when ? then excluded.email_sent_at else theme_reports.email_sent_at end,
                    markdown_path=excluded.markdown_path,
                    html_path=excluded.html_path,
                    error_message=null
                """,
                (
                    schedule_date.isoformat(),
                    theme_id,
                    now,
                    now if email_sent else None,
                    str(markdown_path),
                    str(html_path),
                    1 if email_sent else 0,
                ),
            )

    def mark_failed(self, schedule_date: date, error_message: str, theme_id: str = "default") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                insert into theme_reports(schedule_date, theme_id, status, started_at, error_message)
                values(?, ?, 'failed', ?, ?)
                on conflict(schedule_date, theme_id) do update set
                    status='failed',
                    error_message=excluded.error_message
                """,
                (schedule_date.isoformat(), theme_id, now, error_message[:2000]),
            )

    def get_reminder_sent_at(self, reminder_key: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "select last_sent_at from reminder_emails where reminder_key = ?",
                (reminder_key,),
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            return datetime.fromisoformat(str(row[0]))
        except ValueError:
            return None

    def mark_reminder_sent(self, reminder_key: str, sent_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into reminder_emails(reminder_key, last_sent_at)
                values(?, ?)
                on conflict(reminder_key) do update set
                    last_sent_at=excluded.last_sent_at
                """,
                (reminder_key, sent_at.isoformat(timespec="seconds")),
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
            conn.execute(
                """
                create table if not exists theme_reports(
                    schedule_date text not null,
                    theme_id text not null,
                    status text not null,
                    started_at text,
                    generated_at text,
                    email_sent_at text,
                    markdown_path text,
                    html_path text,
                    error_message text,
                    primary key(schedule_date, theme_id)
                )
                """
            )
            conn.execute(
                """
                create table if not exists reminder_emails(
                    reminder_key text primary key,
                    last_sent_at text not null
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
