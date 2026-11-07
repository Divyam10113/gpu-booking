import time

import pytest
from fastapi.testclient import TestClient

from app import db, gpu
from app.config import Config, User
from app.main import create_app

T = 1_800_000_000  # on a 30-minute boundary
H = 3600


@pytest.fixture
def cfg(tmp_path):
    return Config(
        db_path=str(tmp_path / "test.db"),
        secret_key="test",
        require_approval=True,  # most tests cover the approval flow; see test_auto_approve_*
        users=[
            User("Alice", "alice", admin=True),
            User("Bob", "bob"),
            User("Carol", "carol"),
        ],
    )


@pytest.fixture
def conn(cfg):
    db.init(cfg.db_path)
    c = db.connect(cfg.db_path)
    yield c
    c.close()


def req(conn, cfg, user="Bob", gpus=(0,), start=T + H, end=T + 3 * H, admin=None, now=T):
    return db.create(conn, cfg, user=user, gpus=list(gpus), start=start, end=end, note="", now=now, admin=admin)[0]


# ---------------------------------------------------------------- overlap rules

def test_same_gpu_overlap_with_approved_is_rejected(conn, cfg):
    req(conn, cfg, admin="Alice")
    with pytest.raises(db.BookingError) as e:
        req(conn, cfg, user="Carol", start=T + 2 * H, end=T + 4 * H)
    assert e.value.status == 409


def test_other_gpu_is_fine(conn, cfg):
    req(conn, cfg, gpus=[0], admin="Alice")
    b = req(conn, cfg, user="Carol", gpus=[1])
    assert b.status == "pending"


def test_both_gpus_conflict_with_either_single(conn, cfg):
    req(conn, cfg, gpus=[1], admin="Alice")
    with pytest.raises(db.BookingError):
        req(conn, cfg, user="Carol", gpus=[0, 1])


def test_touching_edges_are_allowed(conn, cfg):
    req(conn, cfg, start=T + H, end=T + 2 * H, admin="Alice")
    b = req(conn, cfg, user="Carol", start=T + 2 * H, end=T + 3 * H, admin="Alice")
    assert b.status == "approved"


def test_pending_requests_may_overlap(conn, cfg):
    a = req(conn, cfg, user="Bob")
    b = req(conn, cfg, user="Carol")
    assert a.status == b.status == "pending"


# ---------------------------------------------------------------- approval

def test_approve_auto_rejects_overlapping_pending(conn, cfg):
    bob = req(conn, cfg, user="Bob", gpus=[0, 1])
    carol = req(conn, cfg, user="Carol", gpus=[1], start=T + 2 * H, end=T + 5 * H)
    other_gpu_later = req(conn, cfg, user="Carol", gpus=[0], start=T + 5 * H, end=T + 6 * H)

    assert [c.id for c in db.preview_approve(conn, bob.id)] == [carol.id]
    approved, losers = db.approve(conn, bob.id, "Alice", now=T)
    assert approved.status == "approved" and approved.decided_by == "Alice"
    assert [l.id for l in losers] == [carol.id]
    rejected = db.get(conn, carol.id)
    assert rejected.status == "rejected" and "Bob" in rejected.admin_note
    assert db.get(conn, other_gpu_later.id).status == "pending"


def test_cannot_approve_into_approved_conflict(conn, cfg):
    p = req(conn, cfg, user="Carol")
    # Admin edits another booking into the same slot after the request was made.
    req(conn, cfg, user="Bob", start=T + 4 * H, end=T + 5 * H, admin="Alice")
    other = db.get(conn, 2)
    db.edit(conn, cfg, other.id, gpus=[0], start=T + H, end=T + 2 * H, note="", admin="Alice", now=T)
    with pytest.raises(db.BookingError):
        db.approve(conn, p.id, "Alice", now=T)


def test_reject_needs_reason(conn, cfg):
    p = req(conn, cfg)
    with pytest.raises(db.BookingError):
        db.reject(conn, p.id, "Alice", "  ", now=T)
    assert db.reject(conn, p.id, "Alice", "Deadline week", now=T).admin_note == "Deadline week"


def test_cannot_decide_twice(conn, cfg):
    p = req(conn, cfg)
    db.approve(conn, p.id, "Alice", now=T)
    with pytest.raises(db.BookingError):
        db.reject(conn, p.id, "Alice", "no", now=T)


def test_stale_pending_expires(conn, cfg):
    p = req(conn, cfg, start=T + H, end=T + 2 * H)
    assert db.expire_stale(conn, T + H + 60) == 0  # started, but still worth approving
    assert db.expire_stale(conn, T + 2 * H) == 1
    assert db.get(conn, p.id).status == "expired"


def test_auto_approve_when_no_clash(conn, cfg):
    cfg.require_approval = False
    b = req(conn, cfg, user="Bob")
    assert b.status == "approved" and b.decided_by is None and b.decided_at is None
    # A clash is still refused outright, so nothing ever waits for an admin.
    with pytest.raises(db.BookingError) as e:
        req(conn, cfg, user="Carol", start=T + 2 * H, end=T + 4 * H)
    assert e.value.status == 409
    assert req(conn, cfg, user="Carol", gpus=[1]).status == "approved"
    # Extensions go straight through too.
    ext, _ = db.request_extension(conn, cfg, b.id, actor="Bob", new_end=T + 4 * H, now=T)
    assert ext.status == "approved"


def test_auto_approve_takes_slot_from_old_pending_request(conn, cfg):
    old = req(conn, cfg, user="Carol")  # made while approval was still required
    cfg.require_approval = False
    _, losers = db.create(conn, cfg, user="Bob", gpus=[0], start=T + H, end=T + 2 * H, note="", now=T)
    assert [l.id for l in losers] == [old.id] and db.get(conn, old.id).status == "rejected"


# ---------------------------------------------------------------- limits / validation

def test_length_limits(conn, cfg):
    with pytest.raises(db.BookingError):
        req(conn, cfg, gpus=[0], start=T + H, end=T + 26 * H)
    with pytest.raises(db.BookingError):
        req(conn, cfg, gpus=[0, 1], start=T + H, end=T + 14 * H)
    assert req(conn, cfg, gpus=[0, 1], start=T + H, end=T + 14 * H, admin="Alice").status == "approved"


def test_validation(conn, cfg):
    for kwargs in [
        dict(start=T - 2 * H, end=T),              # past
        dict(start=T + H + 60, end=T + 2 * H),     # off-grid
        dict(start=T + 2 * H, end=T + H),          # backwards
        dict(gpus=[2]),                            # no such GPU
        dict(gpus=[]),
    ]:
        with pytest.raises(db.BookingError):
            req(conn, cfg, **kwargs)
    # The slot that is already running can still be booked.
    assert req(conn, cfg, start=T, end=T + H, now=T + 600).status == "pending"


# ---------------------------------------------------------------- cancel / extend

def test_end_early_and_cancel(conn, cfg):
    running = req(conn, cfg, start=T, end=T + 4 * H, admin="Alice")
    ended = db.cancel(conn, running.id, actor="Bob", is_admin=False, now=T + H)
    assert ended.status == "approved" and ended.end == T + H

    future = req(conn, cfg, gpus=[1], admin="Alice")
    with pytest.raises(db.BookingError) as e:
        db.cancel(conn, future.id, actor="Carol", is_admin=False, now=T)
    assert e.value.status == 403
    assert db.cancel(conn, future.id, actor="Carol", is_admin=True, now=T).status == "cancelled"


def test_extension_is_a_pending_request(conn, cfg):
    b = req(conn, cfg, start=T + H, end=T + 3 * H, admin="Alice")
    ext, _ = db.request_extension(conn, cfg, b.id, actor="Bob", new_end=T + 5 * H, now=T)
    assert ext.status == "pending" and ext.start == T + 3 * H and ext.extends_id == b.id
    # The whole run still has to respect the limit.
    with pytest.raises(db.BookingError):
        db.request_extension(conn, cfg, b.id, actor="Bob", new_end=T + 30 * H, now=T)


# ---------------------------------------------------------------- HTTP API

@pytest.fixture
def make_client(cfg, monkeypatch):
    monkeypatch.setenv("GPU_MOCK", "bob@0,carol@0")

    def make(name=None, password="secret-pw"):
        """A client logged in as `name`. The first login for a name sets its password."""
        client = TestClient(create_app(cfg, clock=lambda: T, poll=False))
        client.__enter__()
        if name:
            r = client.post("/api/login", json={"name": name, "password": password})
            assert r.status_code == 200, r.text
        return client

    return make


def test_api_flow(make_client):
    bob, carol = make_client("Bob"), make_client("Carol")
    alice = make_client("Alice")

    b1 = bob.post("/api/bookings", json={"gpus": [0], "start": T, "end": T + 2 * H, "note": "sweep"}).json()["booking"]
    c1 = carol.post("/api/bookings", json={"gpus": [0, 1], "start": T + H, "end": T + 3 * H}).json()["booking"]
    assert b1["status"] == c1["status"] == "pending"

    # Non-admins can't use admin endpoints.
    assert bob.get("/api/admin/pending").status_code == 403
    assert bob.post(f"/api/admin/bookings/{b1['id']}/approve", json={}).status_code == 403

    pending = alice.get("/api/admin/pending").json()
    assert [p["id"] for p in pending] == [b1["id"], c1["id"]]
    assert [c["id"] for c in pending[0]["competitors"]] == [c1["id"]]

    r = alice.post(f"/api/admin/bookings/{b1['id']}/approve", json={}).json()
    assert [x["id"] for x in r["auto_rejected"]] == [c1["id"]]

    mine = {b["id"]: b for b in carol.get("/api/my-bookings").json()}
    assert mine[c1["id"]]["status"] == "rejected"
    assert mine[c1["id"]]["admin_note"] == "Slot given to Bob"

    # Live status: Bob holds GPU 0; Carol's mock process there is flagged.
    gpu0 = bob.get("/api/status").json()["gpus"][0]
    assert gpu0["current"]["user"] == "Bob"
    flags = {p["user"]: p["violation"] for p in gpu0["procs"]}
    assert flags == {"bob": False, "carol": True}
    assert gpu0["violation"] is True


def test_logged_out_visitors_see_nothing(make_client):
    stranger = make_client()
    assert stranger.post("/api/bookings", json={"gpus": [0], "start": T, "end": T + H}).status_code == 401
    assert stranger.get("/api/status").status_code == 401  # live processes are for logged-in people only
    assert stranger.get(f"/api/state?start={T}&end={T + H}").status_code == 401


def test_first_login_sets_password_then_it_is_required(make_client):
    stranger = make_client()
    assert stranger.post("/api/login", json={"name": "Bob", "password": "ab"}).status_code == 400  # too short

    bob = make_client("Bob", "bobpass")
    assert stranger.get("/api/config").json()["users"][1] == {"name": "Bob", "admin": False, "has_password": True}

    # Someone else can't log in as Bob any more.
    assert stranger.post("/api/login", json={"name": "Bob", "password": "guess"}).status_code == 403
    assert bob.get("/api/me").json() == {"user": "Bob", "admin": False}


def test_admins_cannot_reset_passwords(make_client):
    make_client("Bob", "bobpass")
    alice = make_client("Alice")
    assert alice.post("/api/admin/users/Bob/reset-password").status_code in (404, 405)
    assert make_client().post("/api/login", json={"name": "Bob", "password": "bobpass"}).status_code == 200


def test_wrong_passwords_lock_the_name(make_client):
    make_client("Bob")
    attacker = make_client()
    for _ in range(10):
        assert attacker.post("/api/login", json={"name": "Bob", "password": "guess"}).status_code == 403
    r = attacker.post("/api/login", json={"name": "Bob", "password": "secret-pw"})
    assert r.status_code == 429  # even the right password waits out the lockout


def test_refuses_placeholder_secret(cfg):
    cfg.secret_key = "change-me"
    with pytest.raises(RuntimeError):
        create_app(cfg, poll=False)


def test_state_bundles_everything(make_client):
    bob, alice = make_client("Bob"), make_client("Alice")
    bob.post("/api/bookings", json={"gpus": [0], "start": T, "end": T + H})
    s = bob.get(f"/api/state?start={T}&end={T + H}").json()
    assert set(s) == {"status", "bookings", "mine"} and len(s["bookings"]) == len(s["mine"]) == 1
    a = alice.get(f"/api/state?start={T}&end={T + H}").json()
    assert set(a) == {"status", "bookings", "mine", "pending", "activity", "usage", "users", "waiting"} and len(a["pending"]) == 1


def test_shared_server_account(cfg, make_client):
    # Dave and Bob share the "bob" account: the live panel shows the account, and a booking by
    # either of them covers processes from that account.
    cfg.users.append(User("Dave", "bob"))
    dave = make_client("Dave")
    alice = make_client("Alice")
    alice.post("/api/bookings", json={"gpus": [0], "start": T, "end": T + H, "user": "Dave"})
    procs = {p["user"]: p for p in dave.get("/api/status").json()["gpus"][0]["procs"]}
    assert procs["bob"]["name"] == "bob" and procs["bob"]["sharers"] == ["Bob", "Dave"]
    assert procs["bob"]["violation"] is False and procs["carol"]["violation"] is True


def test_admin_books_directly_for_someone(make_client):
    alice = make_client("Alice")
    r = alice.post("/api/bookings", json={"gpus": [1], "start": T, "end": T + H, "user": "Carol"}).json()
    assert r["booking"]["status"] == "approved" and r["booking"]["user"] == "Carol"
    bob = make_client("Bob")
    assert bob.post("/api/bookings", json={"gpus": [1], "start": T, "end": T + H, "user": "Carol"}).status_code == 403


# ---------------------------------------------------------------- history / idle release / hours

def test_history_records_who_changed_what(conn, cfg):
    b = req(conn, cfg, user="Bob")
    db.approve(conn, b.id, "Alice", now=T)
    db.edit(conn, cfg, b.id, gpus=[1], start=T + H, end=T + 2 * H, note="", admin="Alice", now=T)
    db.cancel(conn, b.id, actor="Bob", is_admin=False, now=T)
    h = db.history(conn, b.id)
    assert [(x["actor"], x["action"]) for x in h] == [
        ("Bob", "requested"), ("Alice", "approved"), ("Alice", "edited"), ("Bob", "cancelled")]
    assert "GPU 0" in h[2]["detail"] and "GPU 1" in h[2]["detail"]
    assert db.recent_activity(conn)[0]["action"] == "cancelled"


def test_history_of_automatic_changes(conn, cfg):
    p = req(conn, cfg, start=T + H, end=T + 2 * H)
    db.expire_stale(conn, T + 2 * H)
    assert db.history(conn, p.id)[-1] == {"at": T + 2 * H, "actor": None, "action": "expired",
                                          "detail": "Nobody approved it in time"}


def test_idle_booking_is_released_after_an_hour(make_client, cfg):
    clock = {"t": T}
    app = create_app(cfg, clock=lambda: clock["t"], poll=False)
    with TestClient(app):
        conn = db.connect(cfg.db_path)
        bob = req(conn, cfg, user="Bob", gpus=[1], start=T, end=T + 4 * H, admin="Alice")
        carol = req(conn, cfg, user="Carol", gpus=[0], start=T, end=T + 4 * H, admin="Alice")

        def tick(t, procs):  # procs: {gpu: [unix users]}
            clock["t"] = t
            app.state.snapshot = gpu.Snapshot(t, [gpu.GpuInfo(i, "x", 90 if procs.get(i) else 0, 0, 1,
                                                              [gpu.Proc(1, u, 1, "") for u in procs.get(i, [])])
                                                  for i in range(2)])
            app.state.tick()

        tick(T + 30 * 60, {0: ["carol"]})          # Carol is working, Bob hasn't started
        tick(T + 50 * 60, {0: ["carol"], 1: ["carol"]})  # someone else's job doesn't count for Bob
        assert db.get(conn, bob.id).end == T + 4 * H
        tick(T + 60 * 60, {0: ["carol"]})
        assert db.get(conn, bob.id).end == T + H
        assert db.history(conn, bob.id)[-1]["action"] == "released"
        # Carol stops at 1:00; she keeps the GPU until 2:00, then it's freed.
        tick(T + 119 * 60, {})
        assert db.get(conn, carol.id).end == T + 4 * H
        tick(T + 120 * 60, {})
        assert db.get(conn, carol.id).end == T + 2 * H
        conn.close()


def test_idle_release_skips_when_gpus_cant_be_read(make_client, cfg):
    app = create_app(cfg, clock=lambda: T + 3 * H, poll=False)
    with TestClient(app):
        conn = db.connect(cfg.db_path)
        b = req(conn, cfg, user="Bob", start=T, end=T + 4 * H, admin="Alice")
        app.state.snapshot = gpu.Snapshot(T, [], "nvidia-smi failed")
        app.state.tick()
        assert db.get(conn, b.id).end == T + 4 * H
        conn.close()


def test_admin_sees_booked_hours_per_person(make_client):
    alice = make_client("Alice")
    alice.post("/api/bookings", json={"gpus": [0, 1], "start": T, "end": T + 2 * H, "user": "Bob"})
    alice.post("/api/bookings", json={"gpus": [0], "start": T + 2 * H, "end": T + 3 * H, "user": "Carol"})
    people = alice.get(f"/api/admin/usage?start={T}&end={T + 24 * H}").json()["people"]
    assert {p["user"]: p["booked"] for p in people} == {"Bob": 4.0, "Carol": 1.0, "Alice": 0.0}
    assert make_client("Bob").get(f"/api/admin/usage?start={T}&end={T + H}").status_code == 403


def test_used_time_and_unbooked_use_are_counted(make_client, cfg):
    clock = {"t": T}
    app = create_app(cfg, clock=lambda: clock["t"], poll=False)
    with TestClient(app) as client:
        conn = db.connect(cfg.db_path)
        b = req(conn, cfg, user="Bob", gpus=[0, 1], start=T, end=T + 2 * H, admin="Alice")

        def tick(t, gpus):  # gpus: {index: (load %, [unix users])}
            clock["t"] = t
            app.state.snapshot = gpu.Snapshot(t, [gpu.GpuInfo(i, "x", gpus.get(i, (0, []))[0], 0, 1,
                                                              [gpu.Proc(1, u, 1, "") for u in gpus.get(i, (0, []))[1]])
                                                  for i in range(2)])
            app.state.tick()

        tick(T, {0: (90, ["bob"])})                      # first reading only starts the clock
        tick(T + 10, {0: (90, ["bob"])})                 # Bob works on one of his two GPUs: 10 s
        tick(T + 20, {0: (90, ["bob"]), 1: (90, ["bob", "carol"])})  # both: 20 s; Carol on Bob's GPU: 10 s
        tick(T + 30, {0: (0, ["bob"]), 1: (0, ["carol"])})  # Bob idle: 0 s; Carol holding memory still counts
        tick(T + 3 * H, {0: (50, ["carol"])})            # a 3 h gap between readings isn't counted
        tick(T + 3 * H + 10, {0: (50, ["carol"])})       # no booking any more: Carol 10 s unbooked
        assert db.get(conn, b.id).active_secs == 30
        assert db.unbooked_in_range(conn, T, T + 4 * H) == {"carol": 30}
        conn.close()


def test_booking_history_endpoint(make_client):
    bob = make_client("Bob")
    b = bob.post("/api/bookings", json={"gpus": [0], "start": T + H, "end": T + 2 * H}).json()["booking"]
    bob.post(f"/api/bookings/{b['id']}/cancel")
    h = make_client("Carol").get(f"/api/bookings/{b['id']}/history").json()
    assert [x["action"] for x in h] == ["requested", "withdrew"]


def test_zulip_message(make_client, cfg, monkeypatch):
    import urllib.parse
    import urllib.request
    sent = []

    class Resp:
        def close(self): pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: sent.append(req) or Resp())
    cfg.zulip_site, cfg.zulip_bot_email, cfg.zulip_bot_key, cfg.zulip_stream = "https://z.example.com/", "bot@z", "k", "gpu"
    make_client("Bob").post("/api/bookings", json={"gpus": [0], "start": T + H, "end": T + 2 * H})
    assert len(sent) == 1 and sent[0].full_url == "https://z.example.com/api/v1/messages"
    form = urllib.parse.parse_qs(sent[0].data.decode())
    assert form["to"] == ["gpu"] and form["topic"] == ["GPU bookings"] and "Bob" in form["content"][0]


def test_zulip_alert_for_job_on_someone_elses_booking(make_client, cfg, monkeypatch):
    import types
    import app.main as main
    msgs = []
    fake_thread = lambda target, args, daemon: types.SimpleNamespace(start=lambda: msgs.append(args[0]))
    monkeypatch.setattr(main, "threading", types.SimpleNamespace(Thread=fake_thread))
    clock = {"t": T}
    app = create_app(cfg, clock=lambda: clock["t"], poll=False)
    with TestClient(app):
        conn = db.connect(cfg.db_path)
        req(conn, cfg, user="Bob", gpus=[0, 1], start=T, end=T + 4 * H, admin="Alice")

        def tick(t, gpus):  # gpus: {index: [unix users]}
            clock["t"] = t
            app.state.snapshot = gpu.Snapshot(t, [gpu.GpuInfo(i, "x", 90, 0, 1, [gpu.Proc(1, u, 1, "") for u in gpus.get(i, [])])
                                                  for i in range(2)])
            app.state.tick()

        tick(T, {0: ["bob", "carol", "?"]})
        tick(T + 30, {})                       # gone after 30 s: a quick test, no message
        tick(T + 100, {0: ["carol"], 1: ["carol", "stranger"]})
        tick(T + 150, {0: ["carol"], 1: ["carol", "stranger"]})
        assert msgs == []                      # not a full minute yet
        tick(T + 160, {0: ["carol"], 1: ["carol", "stranger"]})
        until = time.strftime("%H:%M", time.localtime(T + 4 * H))
        assert sorted(msgs) == [f"⚠️ carol (Carol) is running on GPUs 0 & 1 during Bob's booking (until {until}).",
                                f"⚠️ stranger is running on GPU 1 during Bob's booking (until {until})."]
        tick(T + 600, {0: ["carol"]})
        tick(T + 900, {0: ["carol"]})
        assert len(msgs) == 2                  # one message per account per booking
        conn.close()


def test_sign_up_needs_admin_approval(make_client, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "account_exists", lambda unix: unix == "erin")
    anon = make_client()
    reg = lambda **kw: anon.post("/api/register", json={"name": "Erin", "unix": "erin", "password": "pw1234", **kw})
    assert reg(unix="nobody").status_code == 400          # no such server account
    assert reg(name="bob").status_code == 409             # taken (any capitalisation)
    assert reg(password="ab").status_code == 400
    assert reg(name="  Erin   Lee ").json() == {"ok": True, "name": "Erin Lee"}
    assert anon.post("/api/register", json={"name": "Dan", "unix": "bob", "password": "pw1234"}).status_code == 200  # shares Bob's account
    names = lambda c: [u["name"] for u in c.get("/api/config").json()["users"]]
    assert "Erin Lee" not in names(anon)
    r = anon.post("/api/login", json={"name": "Erin Lee", "password": "pw1234"})
    assert r.status_code == 403 and "waiting" in r.json()["detail"]

    alice = make_client("Alice")
    assert make_client("Bob").post("/api/admin/users/Erin Lee/approve").status_code == 403
    assert [w["name"] for w in alice.get(f"/api/state?start={T}&end={T + H}").json()["waiting"]] == ["Erin Lee", "Dan"]
    assert alice.post("/api/admin/users/Erin Lee/approve").status_code == 200
    assert alice.post("/api/admin/users/Dan/delete").status_code == 200    # turned down
    assert "Erin Lee" in names(anon) and "Dan" not in names(anon)
    assert anon.post("/api/login", json={"name": "Erin Lee", "password": "pw1234"}).status_code == 200


def test_admin_deletes_user_and_their_bookings_end(make_client, cfg):
    alice, bob = make_client("Alice"), make_client("Bob")
    running = alice.post("/api/bookings", json={"gpus": [0], "start": T, "end": T + 2 * H, "user": "Bob"}).json()["booking"]
    future = alice.post("/api/bookings", json={"gpus": [1], "start": T + 3 * H, "end": T + 4 * H, "user": "Bob"}).json()["booking"]
    assert alice.post("/api/admin/users/Alice/delete").status_code == 400  # not yourself
    assert alice.post("/api/admin/users/Bob/delete").status_code == 200
    assert bob.get("/api/me").json()["user"] is None                         # logged out
    conn = db.connect(cfg.db_path)
    assert db.get(conn, running["id"]).status == db.get(conn, future["id"]).status == "cancelled"
    assert db.history(conn, future["id"])[-1]["detail"] == "Their account was deleted"
    carol = req(conn, cfg, user="Carol", start=T, end=T + 2 * H, admin="Alice")
    db.delete_user(conn, "Carol", "Alice", T + H)                           # mid-booking: ended now
    assert db.get(conn, carol.id).end == T + H and db.get(conn, carol.id).status == "approved"
    conn.close()
    assert "Bob" not in [u["name"] for u in alice.get("/api/config").json()["users"]]


def test_admin_can_make_someone_admin(make_client):
    alice = make_client("Alice")
    assert alice.post("/api/admin/users/Alice/admin", json={"admin": False}).status_code == 400
    assert alice.post("/api/admin/users/Bob/admin", json={"admin": True}).status_code == 200
    assert make_client("Bob").get("/api/me").json()["admin"] is True


def test_change_own_password(make_client):
    bob = make_client("Bob", "oldpass")
    other_browser = make_client("Bob", "oldpass")
    assert bob.post("/api/password", json={"current": "wrong", "new": "newpass"}).status_code == 403
    assert bob.post("/api/password", json={"current": "oldpass", "new": "ab"}).status_code == 400
    assert bob.post("/api/password", json={"current": "oldpass", "new": "newpass"}).status_code == 200
    assert bob.get("/api/me").json()["user"] == "Bob"             # this browser stays in
    assert other_browser.get("/api/me").json()["user"] is None    # others are logged out
    stranger = make_client()
    assert stranger.post("/api/login", json={"name": "Bob", "password": "oldpass"}).status_code == 403
    assert stranger.post("/api/login", json={"name": "Bob", "password": "newpass"}).status_code == 200


def test_job_holding_memory_at_zero_load_counts_as_idle(make_client, cfg):
    clock = {"t": T}
    app = create_app(cfg, clock=lambda: clock["t"], poll=False)
    with TestClient(app):
        conn = db.connect(cfg.db_path)
        b = req(conn, cfg, user="Bob", start=T, end=T + 4 * H, admin="Alice")
        for minutes, util in [(10, 80), (40, 0), (69, 0), (70, 0)]:
            clock["t"] = T + minutes * 60
            app.state.snapshot = gpu.Snapshot(clock["t"], [gpu.GpuInfo(0, "x", util, 9000, 9000, [gpu.Proc(1, "bob", 9000, "jupyter")]),
                                                           gpu.GpuInfo(1, "x", 0, 0, 1)])
            app.state.tick()
            if minutes == 69:
                assert db.get(conn, b.id).end == T + 4 * H  # last busy at 0:10, so not yet an hour
        assert db.get(conn, b.id).end == T + 70 * 60
        conn.close()
