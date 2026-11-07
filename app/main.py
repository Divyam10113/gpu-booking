"""FastAPI app: booking API, admin approval, live GPU status, and the static UI."""

import asyncio
import base64
import json
import pwd
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import db, gpu
from .config import Config, User, check_password, hash_password, load_config

STATIC = Path(__file__).parent / "static"
POLL_SECONDS = 10
ACTIVE_UTIL = 5  # % GPU load at or above which the booker's job counts as working
INTRUDER_GRACE = 60  # seconds someone else's job must stay on a booked GPU before Zulip hears about it
MIN_PASSWORD = 4
MAX_SIGNUPS_PER_HOUR = 10  # across everyone: the link is public, so cap how fast junk sign-ups can pile up
# Wrong-password lockout per name: this many failures within the window locks it for the window.
MAX_FAILURES, FAILURE_WINDOW = 10, 15 * 60
PLACEHOLDER_SECRETS = {"change-me", "dev-secret-change-me", ""}


# ---------------------------------------------------------------- request bodies

class LoginBody(BaseModel):
    name: str
    password: str


class RegisterBody(BaseModel):
    name: str
    unix: str
    password: str


class ForgotBody(BaseModel):
    name: str
    password: str


class AdminFlagBody(BaseModel):
    admin: bool


def server_accounts() -> set[str]:
    """People's login accounts on this server (no system accounts), for the sign-up list."""
    return {p.pw_name for p in pwd.getpwall()
            if 1000 <= p.pw_uid < 65534 and not p.pw_shell.endswith(("nologin", "false"))}


class PasswordBody(BaseModel):
    current: str
    new: str


class BookingBody(BaseModel):
    gpus: list[int]
    start: int
    end: int
    note: str = ""
    user: str | None = None  # admins may book on someone's behalf


class ExtendBody(BaseModel):
    end: int


class EditBody(BaseModel):
    gpus: list[int]
    start: int
    end: int
    note: str = ""


class ApproveBody(BaseModel):
    note: str = ""


class RejectBody(BaseModel):
    reason: str


# ---------------------------------------------------------------- app

def create_app(cfg: Config | None = None, clock=None, poll: bool = True) -> FastAPI:
    cfg = cfg or load_config()
    if cfg.secret_key in PLACEHOLDER_SECRETS:
        raise RuntimeError("Set secret_key in config.toml to a long random string before starting (see README).")
    db.init(cfg.db_path)
    failures: dict[str, list[float]] = {}
    now = clock or (lambda: int(time.time()))
    # Booking id -> last time one of the booker's jobs was seen on its GPUs (in memory: a restart
    # just restarts everyone's idle clock).
    last_active: dict[int, int] = {}
    # (booking id, unix account) -> when that account's job was first seen on the booking's GPUs.
    intruders: dict[tuple[int, str], int] = {}
    alerted: set[tuple[int, str]] = set()  # one Zulip message per account per booking
    started_at = now()
    signups: list[float] = []
    forgot_requests: list[float] = []
    seed = db.connect(cfg.db_path)
    db.seed_users(seed, cfg.users, started_at)
    seed.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.snapshot = gpu.snapshot(cfg.num_gpus)
        task = asyncio.create_task(_poll(app)) if poll else None
        yield
        if task:
            task.cancel()

    async def _poll(app: FastAPI):
        while True:
            await asyncio.sleep(POLL_SECONDS)
            app.state.snapshot = await asyncio.to_thread(gpu.snapshot, cfg.num_gpus)
            await asyncio.to_thread(tick)

    app = FastAPI(title="GPU Booking", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, max_age=60 * 60 * 24 * 90,
                       same_site="lax", session_cookie="gpu_booking")
    app.state.cfg = cfg

    @app.exception_handler(db.BookingError)
    async def booking_error(_req: Request, exc: db.BookingError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    # ------------------------------------------------------------ helpers

    def get_conn():
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def session_user(request: Request, conn: sqlite3.Connection) -> User | None:
        """The logged-in user, as long as their password hasn't been reset since they logged in."""
        user = db.get_user(conn, request.session.get("user"))
        if user is None:
            return None
        hashed = db.password_hash(conn, user.name)
        if hashed is None or request.session.get("pw") != hashed[-16:]:
            return None
        return user

    def current_user(request: Request, conn: Conn) -> User:
        user = session_user(request, conn)
        if user is None:
            raise HTTPException(401, "Log in first")
        return user

    def admin_user(user: User = Depends(current_user)) -> User:
        if not user.admin:
            raise HTTPException(403, "Admins only")
        return user

    def when(ts: int) -> str:
        return time.strftime("%a %d %b %H:%M", time.localtime(ts))

    def describe(b: db.Booking) -> str:
        g = "GPUs " + " & ".join(map(str, b.gpus)) if len(b.gpus) > 1 else f"GPU {b.gpus[0]}"
        return f"{b.user}: {g}, {when(b.start)} → {when(b.end)}"

    def send_notification(text: str):
        """Post to Zulip and/or the webhook. Failures are ignored: bookings work without them."""
        reqs = []
        if cfg.notify_webhook_url:
            reqs.append(urllib.request.Request(cfg.notify_webhook_url, json.dumps({"text": text, "content": text}).encode(),
                                               {"Content-Type": "application/json"}))
        if cfg.zulip_site and cfg.zulip_bot_key:
            auth = base64.b64encode(f"{cfg.zulip_bot_email}:{cfg.zulip_bot_key}".encode()).decode()
            form = urllib.parse.urlencode({"type": "stream", "to": cfg.zulip_stream, "topic": cfg.zulip_topic,
                                           "content": text}).encode()
            reqs.append(urllib.request.Request(cfg.zulip_site.rstrip("/") + "/api/v1/messages", form,
                                               {"Authorization": f"Basic {auth}"}))
        for req in reqs:
            try:
                urllib.request.urlopen(req, timeout=10).close()
            except OSError:
                pass

    def notify(tasks: BackgroundTasks | None, text: str):
        if tasks is None:
            threading.Thread(target=send_notification, args=(text,), daemon=True).start()
        else:
            tasks.add_task(send_notification, text)

    last_tick: dict[str, int | None] = {"t": None}

    def tick():
        """After each GPU reading: count used and unbooked GPU time, and free idle bookings."""
        snap: gpu.Snapshot = app.state.snapshot
        if snap.error or not snap.gpus:
            last_tick["t"] = None  # can't see the GPUs, so count nothing and free nothing
            return
        t = now()
        # Seconds since the last reading; a gap (restart, failed reading) isn't counted.
        dt = t - last_tick["t"] if last_tick["t"] is not None and 0 < t - last_tick["t"] <= 60 else 0
        last_tick["t"] = t
        # A job counts as working only while its GPU is busy: a notebook or server that just
        # holds memory at 0% load is idle.
        working = {(g.index, p.user) for g in snap.gpus if g.util >= ACTIVE_UTIL for p in g.procs}
        present = {(g.index, p.user) for g in snap.gpus for p in g.procs}
        conn = db.connect(cfg.db_path)
        try:
            by_gpu = {}
            for i in range(cfg.num_gpus):
                cur, _ = db.current_and_next(conn, i, t)
                if cur:
                    by_gpu[i] = cur
            running = {b.id: b for b in by_gpu.values()}
            users = db.all_users(conn)
            unix_by_name = {u.name: u.unix for u in users}

            def unix_of(b):
                return unix_by_name.get(b.user)

            if dt:
                hour = t - t % 3600
                with db.tx(conn):
                    for b in running.values():
                        n = sum((g, unix_of(b)) in working for g in b.gpus)
                        if n:
                            db.add_active(conn, b.id, dt * n)
                    for g, unix in present:
                        holder = by_gpu.get(g)
                        if holder is None or unix != unix_of(holder):
                            db.add_unbooked(conn, hour, unix, g, dt)

            # Someone else's job on a booked GPU: tell Zulip once it has stayed a minute.
            on_booked: dict[tuple[int, str], list[int]] = {}
            for g, unix in sorted(present):
                holder = by_gpu.get(g)
                if holder and unix not in ("?", unix_of(holder)):
                    on_booked.setdefault((holder.id, unix), []).append(g)
            for key in [k for k in intruders if k not in on_booked]:
                del intruders[key]
            for key, gs in on_booked.items():
                first = intruders.setdefault(key, t)
                if key in alerted or t - first < INTRUDER_GRACE:
                    continue
                alerted.add(key)
                b, unix = running[key[0]], key[1]
                names = [u.name for u in users if u.unix == unix]
                who = f"{unix} ({', '.join(names)})" if names else unix
                where = "GPUs " + " & ".join(map(str, gs)) if len(gs) > 1 else f"GPU {gs[0]}"
                notify(None, f"⚠️ {who} is running on {where} during {b.user}'s booking "
                             f"(until {time.strftime('%H:%M', time.localtime(b.end))}).")
            alerted.difference_update({k for k in alerted if k[0] not in running})

            for b in running.values():
                if any((g, unix_of(b)) in working for g in b.gpus):
                    last_active[b.id] = t
                    continue
                idle_since = max(b.start, started_at, last_active.get(b.id, 0))
                if cfg.idle_release_minutes and t - idle_since >= cfg.idle_release_minutes * 60:
                    reason = f"No GPU activity from {b.user} for {cfg.idle_release_minutes} min"
                    if db.release(conn, b.id, t, reason):
                        notify(None, f"♻️ Freed #{b.id} ({describe(b)}): {reason.lower()}.")
            for k in [k for k in last_active if k not in running]:
                del last_active[k]
        finally:
            conn.close()

    app.state.tick = tick  # tests call it directly

    def idle_minutes(b: db.Booking, t: int) -> int:
        return (t - max(b.start, started_at, last_active.get(b.id, 0))) // 60

    def out(b: db.Booking) -> dict:
        return asdict(b)

    # ------------------------------------------------------------ pages / session

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        # Browsers re-check the page and its CSS/JS on each load, so updates show up right away.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def accounts(conn) -> list[str]:
        """Accounts people can sign up with: the server's, plus any already in use."""
        return sorted(server_accounts() | {u.unix for u in db.all_users(conn)})

    @app.get("/api/accounts")
    def get_accounts(conn: Conn):
        return accounts(conn)

    @app.get("/api/config")
    def get_config(conn: Conn):
        has_pw = db.users_with_password(conn)
        return {
            "users": [{"name": u.name, "admin": u.admin, "has_password": u.name in has_pw} for u in db.all_users(conn)],
            "num_gpus": cfg.num_gpus,
            "slot_minutes": cfg.slot_minutes,
            "max_hours_single": cfg.max_hours_single,
            "max_hours_all": cfg.max_hours_all,
            "require_approval": cfg.require_approval,
        }

    @app.get("/api/me")
    def me(request: Request, conn: Conn):
        user = session_user(request, conn)
        if user is None:
            return {"user": None, "admin": False}
        return {"user": user.name, "admin": user.admin}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, conn: Conn):
        """Log in. The first login for a name sets that name's password."""
        user = db.get_user(conn, body.name)
        if user is None:
            if db.get_user(conn, body.name, waiting=True):
                raise HTTPException(403, "Your account is waiting for an admin to approve it.")
            raise HTTPException(400, "Unknown user")
        t = time.time()
        recent = [f for f in failures.get(user.name, []) if f > t - FAILURE_WINDOW]
        failures[user.name] = recent
        if len(recent) >= MAX_FAILURES:
            raise HTTPException(429, "Too many wrong tries for this name. Wait 15 minutes and try again.")

        hashed = db.password_hash(conn, user.name)
        if hashed is None:
            if len(body.password) < MIN_PASSWORD:
                raise HTTPException(400, f"Choose a password with at least {MIN_PASSWORD} characters")
            hashed = hash_password(body.password)
            db.set_password_hash(conn, user.name, hashed)
        elif not check_password(body.password, hashed):
            recent.append(t)
            raise HTTPException(403, "Wrong password")
        failures.pop(user.name, None)
        request.session.clear()
        request.session.update({"user": user.name, "pw": hashed[-16:]})
        return {"user": user.name, "admin": user.admin}

    @app.post("/api/register")
    def register(body: RegisterBody, conn: Conn, tasks: BackgroundTasks):
        """Sign up. The account can log in once an admin approves it."""
        name = " ".join(body.name.split())
        unix = body.unix.strip()
        if not 2 <= len(name) <= 40 or not name.isprintable():
            raise HTTPException(400, "Use a name between 2 and 40 characters")
        if unix not in accounts(conn):
            raise HTTPException(400, "Pick your server account from the list")
        if len(body.password) < MIN_PASSWORD:
            raise HTTPException(400, f"Choose a password with at least {MIN_PASSWORD} characters")
        t = time.time()
        signups[:] = [s for s in signups if s > t - 3600]
        if len(signups) >= MAX_SIGNUPS_PER_HOUR:
            raise HTTPException(429, "Too many sign-ups in the last hour. Try again later.")
        db.add_user(conn, name, unix, hash_password(body.password), now())
        signups.append(t)
        notify(tasks, f"👋 New sign-up: {name} (server account {unix}). An admin needs to approve it under Admin → People.")
        return {"ok": True, "name": name}

    @app.post("/api/forgot-password")
    def forgot_password(body: ForgotBody, conn: Conn, tasks: BackgroundTasks):
        """Ask for a new password, chosen by the person. It works once an admin approves it."""
        user = db.get_user(conn, body.name)
        if user is None:
            raise HTTPException(400, "Unknown user")
        if len(body.password) < MIN_PASSWORD:
            raise HTTPException(400, f"Choose a password with at least {MIN_PASSWORD} characters")
        t = time.time()
        forgot_requests[:] = [s for s in forgot_requests if s > t - 3600]
        if len(forgot_requests) >= MAX_SIGNUPS_PER_HOUR:
            raise HTTPException(429, "Too many requests in the last hour. Try again later.")
        db.request_password(conn, user.name, hash_password(body.password), now())
        forgot_requests.append(t)
        notify(tasks, f"🔑 {user.name} forgot their password and chose a new one. An admin needs to approve it "
                      f"under Admin → Password requests. Check with {user.name} first that it was them.")
        return {"ok": True}

    @app.post("/api/password")
    def change_password(body: PasswordBody, request: Request, conn: Conn, user: User = Depends(current_user)):
        """Change your own password. Needs the current one; this browser stays logged in."""
        if not check_password(body.current, db.password_hash(conn, user.name) or ""):
            raise HTTPException(403, "Current password is wrong")
        if len(body.new) < MIN_PASSWORD:
            raise HTTPException(400, f"Choose a password with at least {MIN_PASSWORD} characters")
        hashed = hash_password(body.new)
        db.change_password_hash(conn, user.name, hashed)
        request.session["pw"] = hashed[-16:]  # other browsers logged in as you are logged out
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    # ------------------------------------------------------------ live status

    @app.get("/api/status")
    def status(conn: Conn, request: Request, _user: User = Depends(current_user)):
        # Login required: process command lines can contain tokens or paths people wouldn't want public.
        t = now()
        db.expire_stale(conn, t)
        snap: gpu.Snapshot = request.app.state.snapshot
        live = {g.index: g for g in snap.gpus}
        users = db.all_users(conn)
        result = []
        for i in range(cfg.num_gpus):
            cur, nxt = db.current_and_next(conn, i, t)
            info = live.get(i)
            procs = []
            holder = db.get_user(conn, cur.user) if cur else None
            holder_unix = holder.unix if holder else None
            for p in (info.procs if info else []):
                # Several people can share one server account; then only the account is known.
                sharers = [u.name for u in users if u.unix == p.user]
                procs.append({
                    **asdict(p),
                    "name": sharers[0] if len(sharers) == 1 else p.user,
                    "sharers": sharers,
                    "violation": cur is not None and p.user != holder_unix,
                })
            result.append({
                "index": i,
                "name": info.name if info else None,
                "util": info.util if info else None,
                "mem_used_mib": info.mem_used_mib if info else None,
                "mem_total_mib": info.mem_total_mib if info else None,
                "current": {**out(cur), "idle_min": idle_minutes(cur, t)} if cur else None,
                "next": out(nxt) if nxt else None,
                "procs": procs,
                "violation": any(p["violation"] for p in procs),
                "unbooked_use": cur is None and bool(procs),
            })
        return {"now": t, "taken_at": snap.taken_at, "error": snap.error, "gpus": result,
                "idle_release_minutes": cfg.idle_release_minutes}

    # ------------------------------------------------------------ bookings

    @app.get("/api/bookings")
    def bookings(conn: Conn, start: int, end: int, _user: User = Depends(current_user)):
        return [out(b) for b in db.in_range(conn, start, end)]

    @app.get("/api/my-bookings")
    def my_bookings(conn: Conn, user: User = Depends(current_user)):
        t = now()
        db.expire_stale(conn, t)
        return [out(b) for b in db.for_user(conn, user.name, t - 7 * 86400)]

    @app.post("/api/bookings")
    def create_booking(conn: Conn, body: BookingBody, tasks: BackgroundTasks,
                       user: User = Depends(current_user)):
        admin = user.admin
        owner = user.name
        if body.user and body.user != user.name:
            if not admin:
                raise HTTPException(403, "Only admins can book for someone else")
            if db.get_user(conn, body.user) is None:
                raise HTTPException(400, "Unknown user")
            owner = body.user
        b, losers = db.create(conn, cfg, user=owner, gpus=body.gpus, start=body.start, end=body.end,
                              note=body.note, now=now(), admin=user.name if admin else None)
        note = f" — “{b.note}”" if b.note else ""
        if b.status == "pending":
            notify(tasks, f"🕒 New GPU request #{b.id} — {describe(b)}{note}")
        else:
            by = f" (booked by {user.name})" if user.name != b.user else ""
            notify(tasks, f"📅 Booked #{b.id} — {describe(b)}{by}{note}")
        return {"booking": out(b), "auto_rejected": [out(l) for l in losers]}

    @app.post("/api/bookings/{booking_id}/cancel")
    def cancel_booking(conn: Conn, booking_id: int, tasks: BackgroundTasks, user: User = Depends(current_user)):
        before = db.get(conn, booking_id)
        b = db.cancel(conn, booking_id, actor=user.name, is_admin=user.admin, now=now())
        by = f" by {user.name}" if user.name != b.user else ""
        if b.status == "cancelled" and before.status == "approved":
            notify(tasks, f"🗑️ Cancelled{by} #{b.id} — {describe(before)}")
        elif b.status == "approved":
            notify(tasks, f"✅ Done early{by}: {b.user} freed GPU {db.fmt_gpus(b.gpus).replace(',', ' & ')} at {time.strftime('%H:%M', time.localtime(b.end))}")
        return out(b)

    @app.post("/api/bookings/{booking_id}/extend")
    def extend_booking(conn: Conn, booking_id: int, body: ExtendBody, tasks: BackgroundTasks,
                       user: User = Depends(current_user)):
        admin = user.name if user.admin else None
        b, losers = db.request_extension(conn, cfg, booking_id, actor=user.name, new_end=body.end,
                                         now=now(), admin=admin)
        if b.status == "pending":
            notify(tasks, f"🕒 Extension request #{b.id} — {describe(b)}")
        else:
            notify(tasks, f"⏩ Extended #{booking_id} — {describe(b)}")
        return {"booking": out(b), "auto_rejected": [out(l) for l in losers]}

    @app.patch("/api/bookings/{booking_id}")
    def edit_booking(conn: Conn, booking_id: int, body: EditBody, tasks: BackgroundTasks,
                     admin: User = Depends(admin_user)):
        b = db.edit(conn, cfg, booking_id, gpus=body.gpus, start=body.start, end=body.end,
                    note=body.note, admin=admin.name, now=now())
        notify(tasks, f"✏️ {admin.name} changed #{b.id} — now {describe(b)}")
        return out(b)

    @app.get("/api/bookings/{booking_id}/history")
    def booking_history(conn: Conn, booking_id: int, _user: User = Depends(current_user)):
        db.get(conn, booking_id)  # 404 if missing
        return db.history(conn, booking_id)

    # ------------------------------------------------------------ admin

    @app.get("/api/admin/pending")
    def admin_pending(conn: Conn, _admin: User = Depends(admin_user)):
        db.expire_stale(conn, now())
        return [
            {**out(b), "competitors": [out(c) for c in db.pending_competitors(conn, b)],
             "conflicts": [out(c) for c in db.approved_conflicts(conn, b.gpus, b.start, b.end, b.id)]}
            for b in db.pending(conn)
        ]

    @app.post("/api/admin/users/{name}/approve")
    def admin_approve_user(name: str, conn: Conn, tasks: BackgroundTasks, admin: User = Depends(admin_user)):
        if not db.approve_user(conn, name):
            raise HTTPException(404, "No sign-up waiting under that name")
        notify(tasks, f"✅ {name} can now log in (approved by {admin.name}).")
        return {"ok": True}

    @app.post("/api/admin/users/{name}/password-request/approve")
    def admin_approve_password(name: str, conn: Conn, tasks: BackgroundTasks, admin: User = Depends(admin_user)):
        """Switch someone to the password they asked for. Admins never see or choose it."""
        if not db.approve_password_request(conn, name):
            raise HTTPException(404, "No password request from that person")
        notify(tasks, f"🔑 {name}'s new password works now (approved by {admin.name}).")
        return {"ok": True}

    @app.post("/api/admin/users/{name}/password-request/delete")
    def admin_drop_password(name: str, conn: Conn, _admin: User = Depends(admin_user)):
        if not db.drop_password_request(conn, name):
            raise HTTPException(404, "No password request from that person")
        return {"ok": True}

    @app.post("/api/admin/users/{name}/delete")
    def admin_delete_user(name: str, conn: Conn, admin: User = Depends(admin_user)):
        """Remove someone (or turn down a sign-up). Their running and future bookings end."""
        if name == admin.name:
            raise HTTPException(400, "You can't delete yourself")
        if db.get_user(conn, name) is None and db.get_user(conn, name, waiting=True) is None:
            raise HTTPException(404, "Unknown user")
        db.delete_user(conn, name, admin.name, now())
        return {"ok": True}

    @app.post("/api/admin/users/{name}/admin")
    def admin_set_admin(name: str, body: AdminFlagBody, conn: Conn, admin: User = Depends(admin_user)):
        if name == admin.name:
            raise HTTPException(400, "Ask another admin to change your own admin rights")
        if db.get_user(conn, name) is None:
            raise HTTPException(404, "Unknown user")
        db.set_admin(conn, name, body.admin)
        return {"ok": True}

    @app.get("/api/admin/activity")
    def admin_activity(conn: Conn, _admin: User = Depends(admin_user)):
        return db.recent_activity(conn)

    @app.get("/api/admin/usage")
    def admin_usage(conn: Conn, start: int, end: int, _admin: User = Depends(admin_user)):
        """For [start, end): GPU-hours each person booked and actually used, and GPU-hours each
        server account used without a booking. One GPU for an hour is 1; both GPUs is 2."""
        users = db.all_users(conn)
        booked = {u.name: 0.0 for u in users}
        used = {u.name: 0.0 for u in users}
        for b in db.in_range(conn, start, end):
            if b.status == "approved":
                span = min(b.end, end) - max(b.start, start)
                booked[b.user] = booked.get(b.user, 0.0) + span * len(b.gpus) / 3600
                if start <= b.start < end:  # a booking's use is counted in the week it starts
                    used[b.user] = used.get(b.user, 0.0) + b.active_secs / 3600
        people = [{"user": k, "booked": round(v, 1), "used": round(used.get(k, 0.0), 1)}
                  for k, v in sorted(booked.items(), key=lambda kv: -kv[1])]
        unbooked = [{"account": unix, "names": [u.name for u in users if u.unix == unix],
                     "hours": round(secs / 3600, 1)}
                    for unix, secs in sorted(db.unbooked_in_range(conn, start, end).items(), key=lambda kv: -kv[1])
                    if secs >= 360]  # hide anything under 0.1 h
        return {"people": people, "unbooked": unbooked}

    @app.get("/api/admin/bookings/{booking_id}/preview")
    def admin_preview(conn: Conn, booking_id: int, _admin: User = Depends(admin_user)):
        return {"auto_rejected": [out(l) for l in db.preview_approve(conn, booking_id)]}

    @app.post("/api/admin/bookings/{booking_id}/approve")
    def admin_approve(conn: Conn, booking_id: int, body: ApproveBody, tasks: BackgroundTasks,
                      admin: User = Depends(admin_user)):
        b, losers = db.approve(conn, booking_id, admin.name, now(), body.note)
        notify(tasks, f"✅ Approved #{b.id} — {describe(b)}")
        for l in losers:
            notify(tasks, f"❌ Rejected #{l.id} — {describe(l)} — slot given to {b.user}")
        return {"booking": out(b), "auto_rejected": [out(l) for l in losers]}

    @app.post("/api/admin/bookings/{booking_id}/reject")
    def admin_reject(conn: Conn, booking_id: int, body: RejectBody, tasks: BackgroundTasks,
                     admin: User = Depends(admin_user)):
        b = db.reject(conn, booking_id, admin.name, body.reason, now())
        notify(tasks, f"❌ Rejected #{b.id} — {describe(b)} — {b.admin_note}")
        return out(b)

    # ------------------------------------------------------------ everything in one request

    @app.get("/api/state")
    def state(conn: Conn, request: Request, start: int, end: int, user: User = Depends(current_user)):
        """What the page needs on each refresh, in one call (ngrok's free plan counts requests)."""
        result = {
            "status": status(conn, request, user),
            "bookings": bookings(conn, start, end, user),
            "mine": my_bookings(conn, user),
        }
        if user.admin:
            result["pending"] = admin_pending(conn, user)
            result["activity"] = admin_activity(conn, user)
            result["usage"] = admin_usage(conn, start, end, user)
            has_pw = db.users_with_password(conn)
            result["users"] = [{"name": u.name, "unix": u.unix, "admin": u.admin, "has_password": u.name in has_pw}
                               for u in db.all_users(conn)]
            result["waiting"] = db.waiting_users(conn)
            result["password_requests"] = db.password_requests(conn)
        return result

    return app

