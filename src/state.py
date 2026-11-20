from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator


class StateStore:
    def __init__(self, db_path: Path, read_only: bool = False) -> None:
        self.db_path = db_path
        self.read_only = read_only
        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def is_complete(self, schedule_date: date, require_email: bool = False, theme_id: str = "default") -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select status, email_sent_at, email_required, markdown_path, html_path from theme_reports where schedule_date = ? and theme_id = ?",
                (schedule_date.isoformat(), theme_id),
            ).fetchone()
        if not row or row[0] != "success":
            return False
        if not all(value and Path(value).is_file() for value in row[3:5]):
            return False
        return (bool(row[1]) or not row[2]) if require_email else True

    def get_generated_report(self, schedule_date: date, theme_id: str) -> tuple[Path, Path, bool] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select markdown_path, html_path, email_required from theme_reports "
                "where schedule_date=? and theme_id=? and status in ('generated', 'success')",
                (schedule_date.isoformat(), theme_id),
            ).fetchone()
        if not row or not all(value and Path(value).is_file() for value in row[:2]):
            return None
        return Path(row[0]), Path(row[1]), bool(row[2])

    def mark_generated(self, schedule_date: date, markdown_path: Path, html_path: Path,
                       theme_id: str, email_required: bool = True) -> None:
        with self._connect() as conn:
            conn.execute(
                """insert into theme_reports
                   (schedule_date, theme_id, status, generated_at, markdown_path, html_path, email_required)
                   values (?, ?, 'generated', ?, ?, ?, ?)
                   on conflict(schedule_date, theme_id) do update set
                   status='generated', generated_at=excluded.generated_at,
                   markdown_path=excluded.markdown_path, html_path=excluded.html_path,
                   email_required=excluded.email_required, email_sent_at=null, error_message=null""",
                (schedule_date.isoformat(), theme_id, datetime.now().isoformat(timespec="seconds"),
                 str(markdown_path), str(html_path), int(email_required)),
            )

    def mark_delivery_failed(self, schedule_date: date, theme_id: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "update theme_reports set status='generated', error_message=? where schedule_date=? and theme_id=?",
                (message[:2000], schedule_date.isoformat(), theme_id),
            )

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
            conn.execute("begin immediate")
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
            columns = {row[1] for row in conn.execute("pragma table_info(theme_reports)")}
            if "email_required" not in columns:
                conn.execute("alter table theme_reports add column email_required integer not null default 1")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = (sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True) if self.read_only
                else sqlite3.connect(self.db_path, timeout=30))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


class RunAlreadyActive(FileExistsError):
    pass


@contextmanager
def run_lock(lock_path: Path) -> Iterator[None]:
    """OS lock: released on process exit, never stolen based on file age.

    The small marker file stays on disk; its existence does not mean a run is active.
    Keeping one inode also prevents concurrent waiters locking different files.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunAlreadyActive(f"Another digest run holds {lock_path}") from exc
        locked = True
        # The marker is deliberately opaque. Rewriting/truncating it while
        # locked creates an initialization race for new Windows contenders.
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
