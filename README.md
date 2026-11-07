# GPU Booking

A small web app for sharing the lab's GPU server (2× RTX PRO 6000 Blackwell).

- People **book** time on GPU 0, GPU 1, or both, by dragging along a row on a week timeline. A booking that doesn't clash with anyone else's goes through at once. A clashing one is refused and says who has the slot.
- **Admins** can cancel, end, edit, or extend anyone's booking, book for someone else, and go over the length limits. To have an admin approve every booking instead, set `require_approval = true` in `config.toml`.
- The **Right now** panel shows who holds each GPU, live utilisation and VRAM from `nvidia-smi`, and every process with its Unix user. A process from someone who doesn't hold the booking is flagged in red. Nothing is ever killed.
- If you hold a GPU, the panel shows the `CUDA_VISIBLE_DEVICES` line to paste, and an **I'm done, free it** button.
- A running booking with **no GPU activity from the booker for an hour** is ended automatically, so the GPU goes back to everyone. A job only counts while the GPU is working (at least 5% load); a notebook or server that just holds memory doesn't. The card warns after 10 idle minutes.
- Every change to a booking is **recorded**: who booked, edited, extended, cancelled or ended it, and when. Open a booking and expand **History**. Admins see all of it under **Activity**.
- Admins see, for the week shown, **GPU-hours each person booked vs actually used** (time their jobs kept the GPU busy), and **GPU time each server account took up without a booking**. Each booking's details show how much of it was used.
- Optional **Zulip** messages for new bookings, cancellations, early finishes, extensions, admin edits and automatic releases, and an alert when someone's job stays on a GPU booked by someone else for over a minute (once per account per booking), for example "⚠️ jsmith is running on GPU 0 during Priya's booking (until 15:00)".

## Rules

| | |
|---|---|
| Slot size | 30 min |
| Longest booking, one GPU | 24 h |
| Longest booking, both GPUs | 12 h |
| Unbooked GPU | Free for anyone, shown as "in use without a booking" |
| Clashing booking | Refused, with the name of whoever has the slot |
| Extending | Adds the extra time right after your booking, if it's free |
| Ending early / cancelling | No approval needed |
| Booked but idle | Ended automatically after 60 min with none of the booker's jobs working (≥5% load) on the booked GPUs |

All of these values can be changed in `config.toml`.

## Accounts and logging in

New people click **Sign up** on the login page. They enter their name, pick their account on the server from a list (what `whoami` prints there), and choose a password. The sign-up then waits for an admin, and Zulip gets a message. Admins see it at the top of the admin panel under **Sign-ups waiting** and click **Approve**, or **Delete** for anyone they don't recognise. The list shows the server's user accounts (not system accounts), so it's visible to anyone who opens the sign-up form. At most 10 sign-ups an hour.

Everyone can change their own password with **Change password** at the top. Nobody else can, admins included. Under **People**, admins can make someone an admin or remove that, or delete someone. Deleting ends their current booking and cancels their future ones; past bookings stay in the history. Admins can't delete themselves or change their own admin rights.

If someone forgets their password, they click **Forgot password?** on the login page, pick their name and choose a new password. Admins see it under **Password requests** (and on Zulip) and click **Approve**, after checking with that person that the request was really theirs. The new password then works and the old one stops working. Admins never see or choose anyone's password.

Ten wrong passwords for a name in 15 minutes lock it for 15 minutes.

## Install on the GPU server (no sudo needed)

The app must run on the GPU machine itself, not in a container. It reads `/proc` to find which user owns each GPU process. It runs under your own account, from your home folder. You need Python 3.11 or newer (`python3 --version`).

```bash
mkdir -p ~/gpu-booking
cp -r app deploy requirements.txt config.example.toml ~/gpu-booking/
cd ~/gpu-booking
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
```

Edit `config.toml`:

1. List at least yourself under `[[users]]`, with `admin = true`. `unix` must be the login name on the server. This list is only read the first time the app starts; after that, people sign up on the site and admins manage them there.
2. Set `secret_key` to a random string:
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```
3. Optional: Zulip messages, see below.

Start it:

```bash
deploy/start.sh
```

The app reads `config.toml` only when it starts. After editing it, restart the app with `kill $(cat gpu_booking.pid) && deploy/start.sh` (ngrok keeps running, so the link stays the same).

It runs in the background and keeps going after you log out. The log is in `gpu_booking.log` (warnings and errors only, trimmed to the last 200 lines once it passes 1 MB), and `deploy/stop.sh` stops it. Each run of `deploy/start.sh` also keeps one copy of the database per day in `backups/`, for the last 7 days. To restore one, stop the app, copy it over `gpu_booking.db`, and start it again. Use `PORT=9090 deploy/start.sh` if 8080 is taken.

To bring it back after a reboot or a crash, add two lines to your crontab (`crontab -e`). Use the full path to your folder:

```
@reboot      /home/YOU/gpu-booking/deploy/start.sh >/dev/null 2>&1
*/5 * * * *  /home/YOU/gpu-booking/deploy/start.sh >/dev/null 2>&1
```

The second line checks every 5 minutes and only starts the app if it isn't running.

Open `http://<server-ip>:8080`. If the port isn't reachable from your network, tunnel it:

```bash
ssh -L 8080:localhost:8080 you@gpu-server
```

Then open http://localhost:8080.

Check that live usage works: start something on a GPU (for example `python -c "import torch; x=torch.ones(1, device='cuda:1'); input()"`) and confirm it shows under GPU 1 with your name within 10 seconds. If other people's processes show `?` as the user, the server hides other users' processes from normal accounts (`/proc` mounted with `hidepid`). The live panel then can't tell who is running what; ask whoever runs the server.

If someone with sudo prefers a system service, `deploy/gpu-booking.service` is a systemd unit for that.

## Zulip messages (optional)

1. In Zulip, open **Settings → Personal → Bots → Add a new bot**. Pick **Generic bot**, name it e.g. "GPU Booking", and create it.
2. Make sure the bot can post to the stream you want (for a private stream, subscribe the bot to it).
3. Add these lines to `config.toml` on the server, using the bot's email and API key from the bots page. Put them **above the first `[[users]]`**: lines added at the end of the file count as part of the last person's entry and are ignored.
   ```toml
   zulip_site = "https://yourorg.zulipchat.com"
   zulip_bot_email = "gpu-booking-bot@yourorg.zulipchat.com"
   zulip_bot_key = "the bot's API key"
   zulip_stream = "gpu"
   zulip_topic = "GPU bookings"
   ```
4. Restart the app: `kill $(cat gpu_booking.pid) && deploy/start.sh`.

Keep `config.toml` private (`chmod 600 config.toml`): the key lets anyone post as the bot. `notify_webhook_url` also still works for a Slack or Discord incoming webhook.

## Reach it from anywhere (ngrok)

ngrok gives the app a fixed public HTTPS address, such as `your-name.ngrok-free.app`. The GPU PC connects out to ngrok, so nothing needs to be opened on the server, and no sudo is needed.

1. Sign up at [ngrok.com](https://ngrok.com). Your dashboard shows your free **dev domain** and your **authtoken**.
2. Install ngrok into the app folder:
   ```bash
   mkdir -p bin && curl -sSL https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz | tar -xz -C bin
   bin/ngrok config add-authtoken YOUR_AUTHTOKEN
   ```
3. Create `gpu-booking.env` in the app folder with your dev domain:
   ```bash
   echo 'NGROK_URL=your-name.ngrok-free.app' > gpu-booking.env
   ```
4. Run `deploy/start.sh` again. It starts ngrok next to the app, and the cron lines above keep both running. ngrok's log is in `ngrok.log`.

Things to know about ngrok's free plan:

- The first time each browser opens the link, ngrok shows a warning page. Click **Visit Site**; it doesn't come back for 7 days.
- It has monthly limits on requests and bandwidth (check ngrok's pricing page for the current numbers). To stay inside them, the page refreshes every 2 minutes when opened through ngrok, only while the tab is visible, and right away when you come back to the tab. People on the lab network can use `http://<server-ip>:8080` instead, which refreshes every 15 seconds and doesn't count against ngrok.
- Check that your lab or university allows exposing a service from the GPU PC this way.

## Develop without a GPU

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
GPU_MOCK="bob@0,carol@1" .venv/bin/uvicorn app.main:create_app --factory --reload --port 8080
.venv/bin/python -m pytest
```

`GPU_MOCK` fakes processes as `unixuser@gpu`. Copy `config.example.toml` to `config.toml` first, set any `secret_key` other than the placeholder.

## Layout

- `app/db.py`: SQLite schema, all booking rules (overlaps, approval, expiry, limits) and the change history
- `app/gpu.py`: `nvidia-smi` polling and the mock
- `deploy/start.sh`, `deploy/stop.sh`: run the app and ngrok in the background without sudo
- `app/main.py`: HTTP API and login
- `app/static/`: the single-page UI
- `tests/`: rule and API tests
