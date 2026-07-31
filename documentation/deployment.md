# Deployment & Platform Support

This guide shows you **exactly how to run hermes-router**, step by step. Pick the path that
matches your computer and follow it top to bottom.

---

## First, the big picture

hermes-router has two pieces:

1. **The proxy (`router.py`)** — plain Python; runs on **Windows, macOS, and Linux**.
2. **The `hr` command** — a bash helper (`hr setup`, `hr auth add`, `hr status`, …). Works on
   Linux/macOS and Windows via WSL2 / Git Bash, but **not** in plain Command Prompt / PowerShell.

> Takeaway: the engine runs everywhere. On Windows without bash, use WSL2 or run
> `python router.py` directly.

---

## Which path should I pick?

| Your situation | Go to |
|---|---|
| I'm on **Linux or macOS** | [Linux/macOS install](#path-1--linux--macos-the-hr-way) |
| I'm on **Windows** | [Windows](#path-2--windows) |

After any path, jump to [Check it's working](#check-its-working) and
[Troubleshooting](#troubleshooting).

---

## Before you start: do you have the tools?

**Check if Python is installed:**

```bash
python3 --version      # macOS/Linux
python --version       # Windows
```

You want **3.10 or newer**. If it says "command not found" or an older version:
- **Windows:** download from [python.org](https://www.python.org/downloads/) and, on the
  first install screen, **tick "Add python.exe to PATH"**.
- **macOS:** `brew install python` (or grab it from python.org).
- **Linux:** `sudo apt install python3 python3-venv python3-pip` (Debian/Ubuntu).

You'll also need **at least one free API key** — see
[providers.md](providers.md). Gemini ([aistudio.google.com](https://aistudio.google.com)) is
a good first one: free and quick to create.

---

## Path 1 — Linux / macOS (the `hr` way)

This gives you the full `hr` helper experience.

**Step 1 — one-line install.**

```bash
curl -fsSL https://raw.githubusercontent.com/Shaf2665/Hermes-router/main/get.sh | bash
```

This clones the repo, creates an isolated Python environment, installs dependencies, and
puts the `hr` command on your PATH — all at once.

**Step 2 — run the setup wizard.** It walks you through adding your first key and starting
the router:

```bash
hr setup
```

**Step 3 — confirm it's alive.**

```bash
hr status
```

You should see a dashboard of providers. That's it.

> Prefer to do it manually? `git clone` the repo, `cd` in, run `./install.sh`, then
> `hr setup`.

**Day-to-day commands:** `hr auth add <provider>` (add a key), `hr status` (health),
`hr restart` (apply changes), `hr update` (upgrade). Full list in the
[README](../README.md#commands).

---

## Path 2 — Windows

### 2a. WSL2 (full `hr` experience)

WSL2 runs a real Ubuntu inside Windows, so everything behaves exactly like Linux.

1. Open **PowerShell as Administrator** and run:
   ```powershell
   wsl --install
   ```
   Restart when prompted; it installs Ubuntu and asks you to create a username/password.
2. Open **Ubuntu** from the Start menu (this is your Linux shell).
3. Inside Ubuntu, follow [Path 1 — Linux/macOS](#path-1--linux--macos-the-hr-way). `hr` and
   all its commands now work.

To reach the proxy from a Windows app, use `http://localhost:8319` — WSL2 forwards
localhost automatically.

### 2b. Native Python (no bash)

Run the proxy directly. You won't have the `hr` command, but the proxy works fully.

**Step 1 — get the code** (PowerShell):

```powershell
git clone https://github.com/Shaf2665/Hermes-router.git
cd Hermes-router
```

**Step 2 — create an isolated environment and install dependencies:**

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

You'll see `(venv)` at the start of your prompt — that means it's active.

**Step 3 — set your keys** (for this terminal session):

```powershell
$env:GEMINI_API_KEYS = "paste-your-gemini-key"
$env:PROXY_API_KEYS  = "choose-a-secret-password"
```

**Step 4 — start the router:**

```powershell
python router.py
```

Leave this window open — it's now running on `http://localhost:8319`. Open a **second**
PowerShell window to test it (see [Check it's working](#check-its-working)).

> **Making keys stick:** the `$env:` lines only last for that window. To set them
> permanently, use Windows "Edit the system environment variables", or create a `.env` file
> in the folder (the proxy reads it on startup):
> ```
> GEMINI_API_KEYS=paste-your-gemini-key
> PROXY_API_KEYS=choose-a-secret-password
> ```
> You can also manage keys by editing `auth.json` directly:
> ```json
> { "providers": { "gemini": ["key1"], "openrouter": ["key2"] } }
> ```

---

## Check it's working

Whichever path you took, verify with these checks.

**1. Is it alive?**

```bash
curl http://localhost:8319/health
```
Expected: `{"status":"ok",...}`. (No `curl`? Paste `http://localhost:8319/health` into a web
browser.)

**2. Can it answer a real question?** Replace `sk-router-1` with your `PROXY_API_KEYS` value:

```bash
curl http://localhost:8319/v1/chat/completions \
  -H "Authorization: Bearer sk-router-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"hermes-router","messages":[{"role":"user","content":"Say hi in one word"}]}'
```
Expected: a JSON reply with the model's answer inside `choices[0].message.content`.

**3. Point your app at it.** Use base URL `http://localhost:8319/v1`, API key = your
`PROXY_API_KEYS`, model `hermes-router`. See [usage.md](usage.md) for full examples.

**4. Open the dashboard.** Browse to **`http://localhost:8319/`** for the live monitoring
dashboard — provider health, request log, cache stats, and per-key usage. It asks for your
access key once and remembers it in the browser. Running on a remote box bound to
`HOST=127.0.0.1`? Tunnel it: `ssh -L 8319:127.0.0.1:8319 user@server`, then open
`http://localhost:8319/` locally. See [monitoring.md](monitoring.md).

---

## Troubleshooting

**`Connection refused` / page won't load**
- Is the proxy actually running? (Is the `python router.py` / `hr start` process still up?)

**`401 Unauthorized`**
- Your app's API key doesn't match `PROXY_API_KEYS`. Make them the same and try again.

**`All providers exhausted` (503)**
- No keys loaded, or all of them are rate-limited. Confirm a key is set (`hr auth list`, or
  check your `.env`), and add more — see [providers.md](providers.md).

**`hr: command not found`**
- The `hr` helper is Linux/macOS/WSL only. On native Windows use `python router.py`
  (Path 2b). On Linux/macOS, re-run `./install.sh` or open a new terminal so PATH refreshes.

**Port `8319` already in use**
- Something else is using it. Set a different port: `PORT=8320` in `.env` (and point your app
  at the new port), then restart.

**Windows: `python` isn't recognized**
- Python isn't on your PATH. Reinstall from python.org and tick **"Add python.exe to PATH"**,
  or use WSL2 (Path 2a).

Still stuck? Run `hr doctor` (Linux/macOS/WSL) for an automated diagnosis, or check
`router.log`.

---

## Keep it running (survive reboots)

> **Important:** a plain `hr start` (or `hr setup`'s "start now") runs the proxy as a normal
> background process — it does **not** come back after a server reboot. To survive reboots,
> install it as a service.

**`hr` (Linux):**

```bash
hr service install      # installs a systemd unit + enables it on boot
```

That's it — the proxy now starts on boot and restarts automatically if it crashes, and
`hr restart` manages it. `hr setup` also **offers this as a step**. Run as root (or with
`sudo`) for a system service; without either it installs a per-user service and enables
*lingering* so it still starts at boot. Check it with `hr service status`; remove it with
`hr service uninstall`. (The unit name is `hermes-router`, overridable with
`HERMES_ROUTER_SERVICE`.)

**macOS:** there's no systemd — `hr service install` prints a ready-to-paste **launchd**
plist you drop in `~/Library/LaunchAgents` and `launchctl load -w`.
