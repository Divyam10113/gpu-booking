"""SQLite storage and booking rules.

All times are integer Unix seconds (UTC). `gpus` is stored as a sorted
comma-separated string like "0" or "0,1".
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .config import Config, User

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user        TEXT    NOT NULL,
    gpus        TEXT    NOT NULL,
    start_utc   INTEGER NOT NULL,
    end_utc     INTEGER NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL CHECK (status IN ('pending','approved','rejected','cancelled','expired')),
    extends_id  INTEGER REFERENCES bookings(id),
    decided_by  TEXT,
    decided_at  INTEGER,
    admin_note  TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    active_secs INTEGER NOT NULL DEFAULT 0  -- GPU-seconds the booker's jobs actually worked during it
);
CREATE INDEX IF NOT EXISTS idx_bookings_time ON bookings (start_utc, end_utc);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings (status);
-- Every change to a booking: who did what, when. actor is NULL for automatic changes.
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id  INTEGER NOT NULL REFERENCES bookings(id),
    at          INTEGER NOT NULL,
    actor       TEXT,
    action      TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_booking ON history (booking_id);
-- GPU time used by a server account without holding the booking, per hour.
CREATE TABLE IF NOT EXISTS unbooked (
    hour        INTEGER NOT NULL,
    unix        TEXT    NOT NULL,
    gpu         INTEGER NOT NULL,
    secs        INTEGER NOT NULL,
    PRIMARY KEY (hour, unix, gpu)
);
-- Everyone who can log in. Filled from config.toml's [[users]] the first time, then by sign-ups.
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    unix        TEXT    NOT NULL,
    admin       INTEGER NOT NULL DEFAULT 0,
    approved    INTEGER NOT NULL DEFAULT 1,  -- 0: signed up, waiting for an admin
    created_at  INTEGER NOT NULL
);
-- "Forgot password": the new password someone chose, applied once an admin approves.
CREATE TABLE IF NOT EXISTS password_requests (
    user        TEXT PRIMARY KEY,
    hash        TEXT    NOT NULL,
    at          INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS passwords (
    user        TEXT PRIMARY KEY,
    hash        TEXT NOT NULL
);
"""


class BookingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Booking:
    id: int
    user: str
    gpus: list[int]
    start: int
    end: int
    note: str
    status: str
    extends_id: int | None
    decided_by: str | None
    decided_at: int | None
    admin_note: str
    created_at: int
    active_secs: int = 0

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Booking":
        return cls(
            id=r["id"], user=r["user"], gpus=parse_gpus(r["gpus"]),
            start=r["start_utc"], end=r["end_utc"], note=r["note"], status=r["status"],
            extends_id=r["extends_id"], decided_by=r["decided_by"], decided_at=r["decided_at"],
            admin_note=r["admin_note"], created_at=r["created_at"], active_secs=r["active_secs"],
        )

    def overlaps(self, gpus: list[int], start: int, end: int) -> bool:
        return self.start < end and start < self.end and bool(set(self.gpus) & set(gpus))


def parse_gpus(s: str) -> list[int]:
    return [int(g) for g in s.split(",") if g != ""]


def fmt_gpus(gpus: list[int]) -> str:
    return ",".join(str(g) for g in sorted(set(gpus)))


def summary(gpus: list[int], start: int, end: int) -> str:
    """e.g. "GPU 0, Wed 05 Nov 14:00–16:00" in the server's time zone."""
    g = "GPUs " + " & ".join(map(str, gpus)) if len(gpus) > 1 else f"GPU {gpus[0]}"
    a, b = time.localtime(start), time.localtime(end)
    same_day = a[:3] == b[:3] or (end - start <= 86400 and time.strftime("%H:%M", b) == "00:00")
    tail = time.strftime("%H:%M" if same_day else "%a %d %b %H:%M", b)
    return f"{g}, {time.strftime('%a %d %b %H:%M', a)}{'–' if same_day else ' to '}{tail}"


def log(conn, booking_id: int, actor: str | None, action: str, detail: str, at: int) -> None:
    conn.execute("INSERT INTO history (booking_id, at, actor, action, detail) VALUES (?, ?, ?, ?, ?)",
                 (booking_id, at, actor, action, detail))


def history(conn, booking_id: int) -> list[dict]:
    rows = conn.execute("SELECT at, actor, action, detail FROM history WHERE booking_id = ? ORDER BY id",
                        (booking_id,)).fetchall()
    return [dict(r) for r in rows]


def recent_activity(conn, limit: int = 30) -> list[dict]:
    """Latest changes across all bookings, newest first."""
    rows = conn.execute(
        "SELECT h.booking_id, h.at, h.actor, h.action, h.detail, b.user FROM history h"
        " JOIN bookings b ON b.id = h.booking_id ORDER BY h.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_active(conn, booking_id: int, secs: int) -> None:
    conn.execute("UPDATE bookings SET active_secs = active_secs + ? WHERE id = ?", (secs, booking_id))


def add_unbooked(conn, hour: int, unix: str, gpu: int, secs: int) -> None:
    conn.execute("INSERT INTO unbooked (hour, unix, gpu, secs) VALUES (?, ?, ?, ?)"
                 " ON CONFLICT (hour, unix, gpu) DO UPDATE SET secs = secs + excluded.secs",
                 (hour, unix, gpu, secs))


def unbooked_in_range(conn, start: int, end: int) -> dict[str, int]:
    """GPU-seconds each server account used without a booking, in hours starting in [start, end)."""
    rows = conn.execute("SELECT unix, SUM(secs) AS s FROM unbooked WHERE hour >= ? AND hour < ? GROUP BY unix",
                        (start, end)).fetchall()
    return {r["unix"]: r["s"] for r in rows}


def connect(path: str) -> sqlite3.Connection:
    """One connection per request; SQLite's own locking keeps writers apart."""
    # FastAPI may open it in one worker thread and use it in another, never concurrently.
    conn = sqlite3.connect(path, isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: str) -> None:
    conn = connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # Databases made before usage tracking lack this column.
    if "active_secs" not in {r["name"] for r in conn.execute("PRAGMA table_info(bookings)")}:
        conn.execute("ALTER TABLE bookings ADD COLUMN active_secs INTEGER NOT NULL DEFAULT 0")
    conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    """Write transaction that takes the DB lock up front, so check-then-write is atomic."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- users

def seed_users(conn, users: list[User], at: int) -> None:
    """Copy config.toml's people into an empty users table. After that the database is the list."""
    with tx(conn):
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
            conn.executemany("INSERT OR IGNORE INTO users (name, unix, admin, created_at) VALUES (?, ?, ?, ?)",
                             [(u.name, u.unix, int(u.admin), at) for u in users])


def _user(r) -> User:
    return User(name=r["name"], unix=r["unix"], admin=bool(r["admin"]))


def all_users(conn) -> list[User]:
    """Everyone who can log in (approved), in sign-up order."""
    return [_user(r) for r in conn.execute("SELECT * FROM users WHERE approved = 1 ORDER BY id")]


def waiting_users(conn) -> list[dict]:
    """Sign-ups an admin hasn't approved yet."""
    return [{"name": r["name"], "unix": r["unix"], "at": r["created_at"]}
            for r in conn.execute("SELECT * FROM users WHERE approved = 0 ORDER BY id")]


def get_user(conn, name: str | None, waiting: bool = False) -> User | None:
    """An approved user by exact name (sign-up refuses names that differ only in case).
    waiting=True looks for a sign-up that is still waiting instead."""
    if not name:
        return None
    r = conn.execute("SELECT * FROM users WHERE name = ? COLLATE BINARY AND approved = ?",
                     (name, 0 if waiting else 1)).fetchone()
    return _user(r) if r else None


def add_user(conn, name: str, unix: str, hashed: str, at: int) -> None:
    """A sign-up: stored with its password, waiting for an admin to approve it."""
    with tx(conn):
        try:
            conn.execute("INSERT INTO users (name, unix, admin, approved, created_at) VALUES (?, ?, 0, 0, ?)",
                         (name, unix, at))
        except sqlite3.IntegrityError:
            raise BookingError("That name is taken. Pick another, or log in if it's you.", 409)
        conn.execute("INSERT OR REPLACE INTO passwords (user, hash) VALUES (?, ?)", (name, hashed))


def approve_user(conn, name: str) -> bool:
    return conn.execute("UPDATE users SET approved = 1 WHERE name = ? COLLATE BINARY AND approved = 0",
                        (name,)).rowcount == 1


def set_admin(conn, name: str, admin: bool) -> None:
    conn.execute("UPDATE users SET admin = ? WHERE name = ? COLLATE BINARY", (int(admin), name))


def delete_user(conn, name: str, actor: str, now: int) -> None:
    """Remove a person: end their running booking now, cancel future ones and open requests.
    Past bookings stay in the history under their name."""
    with tx(conn):
        rows = conn.execute("SELECT * FROM bookings WHERE user = ? AND status IN ('approved','pending') AND end_utc > ?",
                            (name, now)).fetchall()
        for b in map(Booking.from_row, rows):
            if b.status == "approved" and b.start < now:
                conn.execute("UPDATE bookings SET end_utc = ? WHERE id = ?", (now, b.id))
                log(conn, b.id, actor, "ended early", "Their account was deleted", now)
            else:
                conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (b.id,))
                log(conn, b.id, actor, "cancelled", "Their account was deleted", now)
        conn.execute("DELETE FROM passwords WHERE user = ?", (name,))
        conn.execute("DELETE FROM password_requests WHERE user = ?", (name,))
        conn.execute("DELETE FROM users WHERE name = ? COLLATE BINARY", (name,))


# ---------------------------------------------------------------- passwords

def password_hash(conn, user: str) -> str | None:
    row = conn.execute("SELECT hash FROM passwords WHERE user = ?", (user,)).fetchone()
    return row["hash"] if row else None


def users_with_password(conn) -> set[str]:
    return {r["user"] for r in conn.execute("SELECT user FROM passwords")}


def set_password_hash(conn, user: str, hashed: str) -> None:
    """Store a first password. Fails if someone set one in the meantime."""
    try:
        conn.execute("INSERT INTO passwords (user, hash) VALUES (?, ?)", (user, hashed))
    except sqlite3.IntegrityError:
        raise BookingError("A password was already set for this name", 409)


def request_password(conn, user: str, hashed: str, at: int) -> None:
    """Someone forgot their password and chose a new one. A newer request replaces an older one."""
    conn.execute("INSERT OR REPLACE INTO password_requests (user, hash, at) VALUES (?, ?, ?)", (user, hashed, at))


def password_requests(conn) -> list[dict]:
    return [{"name": r["user"], "at": r["at"]} for r in conn.execute("SELECT user, at FROM password_requests ORDER BY at")]


def approve_password_request(conn, user: str) -> bool:
    """Make the requested password the real one. Logs that person out everywhere."""
    with tx(conn):
        r = conn.execute("SELECT hash FROM password_requests WHERE user = ?", (user,)).fetchone()
        if r is None:
            return False
        conn.execute("INSERT OR REPLACE INTO passwords (user, hash) VALUES (?, ?)", (user, r["hash"]))
        conn.execute("DELETE FROM password_requests WHERE user = ?", (user,))
    return True


def drop_password_request(conn, user: str) -> bool:
    return conn.execute("DELETE FROM password_requests WHERE user = ?", (user,)).rowcount == 1


def change_password_hash(conn, user: str, hashed: str) -> None:
    conn.execute("UPDATE passwords SET hash = ? WHERE user = ?", (hashed, user))




# ---------------------------------------------------------------- queries

def get(conn: sqlite3.Connection, booking_id: int) -> Booking:
    row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if row is None:
        raise BookingError("Booking not found", 404)
    return Booking.from_row(row)


def _overlapping(conn, status: str, gpus: list[int], start: int, end: int, exclude: int | None = None) -> list[Booking]:
    rows = conn.execute(
        "SELECT * FROM bookings WHERE status = ? AND start_utc < ? AND end_utc > ? AND id IS NOT ?"
        " ORDER BY start_utc",
        (status, end, start, exclude),
    ).fetchall()
    return [b for b in map(Booking.from_row, rows) if b.overlaps(gpus, start, end)]


def approved_conflicts(conn, gpus, start, end, exclude=None) -> list[Booking]:
    return _overlapping(conn, "approved", gpus, start, end, exclude)


def pending_competitors(conn, b: Booking) -> list[Booking]:
    return _overlapping(conn, "pending", b.gpus, b.start, b.end, b.id)


def in_range(conn, start: int, end: int) -> list[Booking]:
    rows = conn.execute(
        "SELECT * FROM bookings WHERE status IN ('approved','pending') AND start_utc < ? AND end_utc > ?"
        " ORDER BY start_utc",
        (end, start),
    ).fetchall()
    return [Booking.from_row(r) for r in rows]


def for_user(conn, user: str, since: int) -> list[Booking]:
    rows = conn.execute(
        "SELECT * FROM bookings WHERE user = ? AND (end_utc > ? OR created_at > ?) ORDER BY start_utc",
        (user, since, since),
    ).fetchall()
    return [Booking.from_row(r) for r in rows]


def pending(conn) -> list[Booking]:
    rows = conn.execute("SELECT * FROM bookings WHERE status = 'pending' ORDER BY created_at").fetchall()
    return [Booking.from_row(r) for r in rows]


def current_and_next(conn, gpu: int, now: int) -> tuple[Booking | None, Booking | None]:
    rows = conn.execute(
        "SELECT * FROM bookings WHERE status = 'approved' AND end_utc > ? ORDER BY start_utc", (now,)
    ).fetchall()
    mine = [b for b in map(Booking.from_row, rows) if gpu in b.gpus]
    current = next((b for b in mine if b.start <= now), None)
    upcoming = next((b for b in mine if b.start > now), None)
    return current, upcoming


# ---------------------------------------------------------------- rules

def _validate(cfg: Config, gpus: list[int], start: int, end: int, now: int, as_admin: bool) -> list[int]:
    gpus = sorted(set(gpus))
    if not gpus or any(g < 0 or g >= cfg.num_gpus for g in gpus):
        raise BookingError(f"Pick GPUs between 0 and {cfg.num_gpus - 1}")
    if end <= start:
        raise BookingError("End must be after start")
    slot = cfg.slot_minutes * 60
    if start % slot or end % slot:
        raise BookingError(f"Times must be on a {cfg.slot_minutes}-minute boundary")
    # The slot that is currently running counts as "now".
    if start < now - now % slot:
        raise BookingError("Can't book time in the past")
    if not as_admin:
        hours = (end - start) / 3600
        limit = cfg.max_hours_all if len(gpus) == cfg.num_gpus else cfg.max_hours_single
        if hours > limit:
            which = "all GPUs" if len(gpus) == cfg.num_gpus else "one GPU"
            raise BookingError(f"Requests for {which} are limited to {limit:g} hours")
    return gpus


def _check_free(conn, gpus, start, end, exclude=None):
    clash = approved_conflicts(conn, gpus, start, end, exclude)
    if clash:
        c = clash[0]
        raise BookingError(f"Overlaps an approved booking by {c.user} on GPU {fmt_gpus(c.gpus)}", 409)


def _set_status(conn, b_id: int, status: str, admin: str | None, now: int, admin_note: str = ""):
    conn.execute(
        "UPDATE bookings SET status = ?, decided_by = ?, decided_at = ?, admin_note = ? WHERE id = ?",
        (status, admin, now, admin_note, b_id),
    )


def _approve_locked(conn, b: Booking, admin: str, now: int, note: str = "") -> list[Booking]:
    _check_free(conn, b.gpus, b.start, b.end, b.id)
    losers = pending_competitors(conn, b)
    _set_status(conn, b.id, "approved", admin, now, note)
    log(conn, b.id, admin, "approved", note, now)
    _reject_losers(conn, losers, b, admin, now)
    return losers


def _reject_losers(conn, losers: list[Booking], winner: Booking, admin: str | None, now: int):
    for l in losers:
        _set_status(conn, l.id, "rejected", admin, now, f"Slot given to {winner.user}")
        log(conn, l.id, admin, "rejected", f"Slot given to {winner.user} (#{winner.id})", now)


def create(conn, cfg: Config, *, user: str, gpus: list[int], start: int, end: int, note: str,
           now: int, admin: str | None = None, extends_id: int | None = None,
           actor: str | None = None) -> tuple[Booking, list[Booking]]:
    """Create a booking. It's approved at once when `admin` is given, or when approval isn't
    required (it can't clash: clashes with approved bookings are refused). Otherwise it waits
    for an admin.

    `actor` is who made it, when that isn't `admin` or `user` (default: admin, else user).
    Returns the booking and any pending requests that were auto-rejected.
    """
    actor = actor or admin or user
    gpus = _validate(cfg, gpus, start, end, now, as_admin=admin is not None)
    with tx(conn):
        _check_free(conn, gpus, start, end)
        cur = conn.execute(
            "INSERT INTO bookings (user, gpus, start_utc, end_utc, note, status, extends_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (user, fmt_gpus(gpus), start, end, note.strip(), extends_id, now),
        )
        b = get(conn, cur.lastrowid)
        instant = admin is not None or not cfg.require_approval
        what = summary(gpus, start, end) + (f" for {user}" if actor != user else "")
        if extends_id:
            what += f" (extends #{extends_id})"
        log(conn, b.id, actor, "booked" if instant else "requested", what, now)
        if instant:
            # Nobody had to decide anything, so decided_by stays empty.
            conn.execute("UPDATE bookings SET status = 'approved' WHERE id = ?", (b.id,))
            losers = pending_competitors(conn, b)
            _reject_losers(conn, losers, b, admin, now)
        else:
            losers = []
    return get(conn, b.id), losers


def preview_approve(conn, booking_id: int) -> list[Booking]:
    """Pending requests that would be auto-rejected if this one were approved."""
    b = get(conn, booking_id)
    if b.status != "pending":
        raise BookingError(f"Request is already {b.status}")
    _check_free(conn, b.gpus, b.start, b.end, b.id)
    return pending_competitors(conn, b)


def approve(conn, booking_id: int, admin: str, now: int, note: str = "") -> tuple[Booking, list[Booking]]:
    with tx(conn):
        b = get(conn, booking_id)
        if b.status != "pending":
            raise BookingError(f"Request is already {b.status}", 409)
        if b.end <= now:
            raise BookingError("This request is already over", 409)
        losers = _approve_locked(conn, b, admin, now, note)
    return get(conn, booking_id), losers


def reject(conn, booking_id: int, admin: str, reason: str, now: int) -> Booking:
    reason = reason.strip()
    if not reason:
        raise BookingError("Give a reason for rejecting")
    with tx(conn):
        b = get(conn, booking_id)
        if b.status != "pending":
            raise BookingError(f"Request is already {b.status}", 409)
        _set_status(conn, b.id, "rejected", admin, now, reason)
        log(conn, b.id, admin, "rejected", reason, now)
    return get(conn, booking_id)


def cancel(conn, booking_id: int, *, actor: str, is_admin: bool, now: int) -> Booking:
    """Withdraw a pending request, or cancel / end early an approved booking."""
    with tx(conn):
        b = get(conn, booking_id)
        if b.user != actor and not is_admin:
            raise BookingError("That's not your booking", 403)
        if b.status == "pending":
            conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (b.id,))
            log(conn, b.id, actor, "withdrew", "", now)
        elif b.status == "approved" and b.end > now:
            if b.start < now:
                # Already running: end it now and keep the record.
                conn.execute("UPDATE bookings SET end_utc = ? WHERE id = ?", (now, b.id))
                log(conn, b.id, actor, "ended early", f"was booked until {time.strftime('%H:%M', time.localtime(b.end))}", now)
            else:
                conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (b.id,))
                log(conn, b.id, actor, "cancelled", "", now)
        else:
            raise BookingError("Nothing to cancel", 409)
    return get(conn, booking_id)


def request_extension(conn, cfg: Config, booking_id: int, *, actor: str, new_end: int, now: int,
                      admin: str | None = None) -> tuple[Booking, list[Booking]]:
    """Ask for more time right after an approved booking, as a separate linked request."""
    b = get(conn, booking_id)
    if b.user != actor and admin is None:
        raise BookingError("That's not your booking", 403)
    if b.status != "approved" or b.end <= now:
        raise BookingError("Only current or upcoming approved bookings can be extended", 409)
    if new_end <= b.end:
        raise BookingError("New end must be later than the current end")
    # Length limits apply to the whole run, not just the extra piece.
    if admin is None:
        _validate(cfg, b.gpus, b.start - b.start % (cfg.slot_minutes * 60), new_end, b.start, as_admin=False)
    ext, losers = create(conn, cfg, user=b.user, gpus=b.gpus, start=b.end, end=new_end,
                         note=f"Extension of #{b.id}", now=now, admin=admin, extends_id=b.id, actor=actor)
    verb = "extended" if ext.status == "approved" else "asked to extend"
    log(conn, b.id, actor, verb, f"until {time.strftime('%a %d %b %H:%M', time.localtime(new_end))} (#{ext.id})", now)
    return ext, losers


def edit(conn, cfg: Config, booking_id: int, *, gpus: list[int], start: int, end: int, note: str,
         admin: str, now: int) -> Booking:
    """Admin change of an approved or pending booking's GPUs/times."""
    gpus = _validate(cfg, gpus, start, end, min(now, start), as_admin=True)
    with tx(conn):
        b = get(conn, booking_id)
        if b.status not in ("approved", "pending"):
            raise BookingError(f"Can't edit a {b.status} booking", 409)
        if b.status == "approved":
            _check_free(conn, gpus, start, end, b.id)
        conn.execute(
            "UPDATE bookings SET gpus = ?, start_utc = ?, end_utc = ?, note = ?, decided_by = ?, decided_at = ?"
            " WHERE id = ?",
            (fmt_gpus(gpus), start, end, note.strip(), admin if b.status == "approved" else b.decided_by,
             now if b.status == "approved" else b.decided_at, b.id),
        )
        changes = []
        if (b.gpus, b.start, b.end) != (gpus, start, end):
            changes.append(f"was {summary(b.gpus, b.start, b.end)} · now {summary(gpus, start, end)}")
        if b.note != note.strip():
            changes.append(f"note: “{note.strip()}”")
        log(conn, b.id, admin, "edited", "; ".join(changes) or "no change", now)
    return get(conn, booking_id)


def release(conn, booking_id: int, now: int, reason: str) -> Booking | None:
    """End a running booking now on the system's behalf (e.g. nothing ran on it for too long)."""
    with tx(conn):
        b = get(conn, booking_id)
        if b.status != "approved" or not b.start < now < b.end:
            return None
        conn.execute("UPDATE bookings SET end_utc = ? WHERE id = ?", (now, b.id))
        log(conn, b.id, None, "released", reason, now)
    return get(conn, booking_id)


def expire_stale(conn, now: int) -> int:
    """Pending requests whose whole window has passed can no longer be approved."""
    with tx(conn):
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM bookings WHERE status = 'pending' AND end_utc <= ?", (now,))]
        for i in ids:
            conn.execute("UPDATE bookings SET status = 'expired', decided_at = ? WHERE id = ?", (now, i))
            log(conn, i, None, "expired", "Nobody approved it in time", now)
    return len(ids)
