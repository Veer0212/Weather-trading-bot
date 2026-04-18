# Running on Windows + Pushing to GitHub

This doc fixes the "Python was not found" error you hit and walks you through pushing the bot to GitHub with an hourly auto-running workflow.

---

## Part 1 — Fix the "Python was not found" error on Windows

The errors you saw happen because (a) Python isn't actually installed (the "Python was not found" message is Windows' Microsoft Store stub), and (b) the commands I gave were Mac/Linux style. Here is the Windows version.

### 1.1 Install Python

1. Open **https://www.python.org/downloads/** in your browser.
2. Click the big yellow **Download Python 3.12.x** button (or whichever is latest).
3. Run the downloaded `.exe`.
4. **CRITICAL**: on the first install screen, check the box **"Add python.exe to PATH"** at the bottom. If you skip this, nothing below will work.
5. Click **Install Now** and wait ~1 minute.
6. **Close your Command Prompt window and open a new one** — PATH changes only apply to new windows.

Verify the install:

```cmd
python --version
pip --version
```

Both should print a version number. If you still get "Python was not found", open Windows Settings → Apps → Advanced app settings → App execution aliases and **turn off** both `python.exe` and `python3.exe` aliases.

### 1.2 Run the bot with the correct Windows commands

Open a new Command Prompt:

```cmd
cd C:\Users\Veer Vihaan\Downloads\polymarket_weather_bot
pip install -r requirements.txt
python run.py --bankroll 1000 --interval 180
```

Leave that window running. Open a **second** Command Prompt (separate window):

```cmd
cd C:\Users\Veer Vihaan\Downloads\polymarket_weather_bot
python -m http.server 8787
```

Now open your browser and go to **http://localhost:8787/dashboard.html**.

Note: on Windows, use `python` not `python3`, and use `start http://...` instead of `open http://...` if you want to open URLs from the command line.

---

## Part 2 — Push to GitHub with hourly auto-run

### 2.1 Create the repo

1. Go to **https://github.com/new**.
2. Name it something like `polymarket-weather-bot`.
3. **Leave it empty** — do NOT initialize with README, .gitignore, or license. We're pushing code that already has those.
4. Click **Create repository**.
5. Copy the HTTPS URL from the "Quick setup" section. It looks like `https://github.com/YOUR-USERNAME/polymarket-weather-bot.git`.

### 2.2 Install Git if you don't have it

Check with `git --version` in cmd. If not installed, grab it from **https://git-scm.com/download/win** and use default options during install.

### 2.3 Push the code

In Command Prompt, from the bot folder:

```cmd
cd C:\Users\Veer Vihaan\Downloads\polymarket_weather_bot

git init
git branch -M main

git add .
git commit -m "Initial commit: Polymarket weather bot"

git remote add origin https://github.com/YOUR-USERNAME/polymarket-weather-bot.git
git push -u origin main
```

Replace `YOUR-USERNAME/polymarket-weather-bot` with your actual repo path. The first `git push` will prompt you to authenticate — Git will open a browser for GitHub login automatically on modern Git installs. If it asks for a password, use a **Personal Access Token** (github.com → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token with `repo` scope).

### 2.4 Set your local git identity (one-time)

If git complains about author identity on commit, run:

```cmd
git config --global user.name  "Veer Vihaan Sheth"
git config --global user.email "veersiddharthsheth@gmail.com"
```

---

## Part 3 — Enable the automated workflows

### 3.1 GitHub Actions (hourly runs) — should just work

Once the push succeeds, Actions is already wired up. Go to:

**`https://github.com/YOUR-USERNAME/polymarket-weather-bot/actions`**

You'll see two workflows:
- **Polymarket Weather Bot** — runs every hour on the `:07` minute, does one scan cycle, commits new trades + dashboard state back to the repo
- **Deploy dashboard to GitHub Pages** — publishes the dashboard whenever it changes

The first run will trigger automatically on your initial push. To manually trigger a run right now:

1. Click **Polymarket Weather Bot** in the left sidebar
2. Click **Run workflow** → **Run workflow**

You should see a green check within ~2 minutes.

### 3.2 Give Actions permission to commit back (one-time)

By default, GITHUB_TOKEN on a new repo can't push back to the same repo. Enable it:

1. Go to **Settings → Actions → General** in your repo
2. Scroll to **Workflow permissions**
3. Select **Read and write permissions**
4. Click **Save**

### 3.3 Enable GitHub Pages (for the live dashboard)

1. Go to **Settings → Pages** in your repo
2. Under **Source**, select **GitHub Actions**
3. Save

After the first Pages workflow run succeeds, your live dashboard will be at:

**`https://YOUR-USERNAME.github.io/polymarket-weather-bot/dashboard.html`**

It updates automatically every time the bot commits new data (about once per hour).

---

## Part 4 — Monitoring

### On GitHub

- **Actions tab** — every hourly run appears as a job. Click any run to see logs. A successful run ends with a summary like `Markets scanned: 12  Open trades: 3  Realized P&L: $0.00`.
- **Commits** — look for commits by `polymarket-weather-bot[bot]` titled `bot: cycle <timestamp>`. Each one contains the updated `logs/trades.jsonl` and `logs/dashboard_data.json`.
- **Live dashboard** — `https://YOUR-USERNAME.github.io/polymarket-weather-bot/dashboard.html`

### Locally

- `logs/trades.jsonl` — one JSON object per line, append-only record of every trade ever logged
- `logs/dashboard_data.json` — latest snapshot the dashboard reads
- `logs/bot.log` — rolling bot log (local runs only; Actions writes to stdout)

---

## Part 5 — Troubleshooting

| Symptom | Fix |
|---|---|
| `git push` fails with "authentication failed" | Use a Personal Access Token as your password, not your GitHub login password. See §2.3. |
| Actions workflow fails with "remote: Permission denied" | You haven't done §3.2 — set workflow permissions to Read and write. |
| Pages deploy succeeds but dashboard shows "waiting for first cycle…" | Wait for one hourly bot run to complete and commit logs. Then the Pages workflow will re-deploy with the data. |
| `python` still not found after install | Re-install with "Add python.exe to PATH" checked, or add it manually: Settings → System → About → Advanced system settings → Environment Variables → Path → Edit → add your Python install folder (e.g. `C:\Users\Veer Vihaan\AppData\Local\Programs\Python\Python312`). |
| Actions runs but no trades appear | Normal early on. The bot only trades when it finds an ≥6pp edge on a market with ≥$500 volume. Weather markets can be thin. Let it run for a full day and check the **Markets scanned** count in dashboard_data.json. |
| Dashboard on GitHub Pages shows stale data | GitHub Pages has CDN caching — add `?t=<random>` to the URL to force refresh, or wait ~5 minutes. |

---

## What's in the repo

```
polymarket-weather-bot/
├── .github/workflows/
│   ├── bot.yml              # hourly cron that runs the bot
│   └── pages.yml            # deploys dashboard to GitHub Pages
├── .gitignore
├── README.md
├── SETUP_GUIDE.md           # paper + live trading setup
├── PUSH_INSTRUCTIONS.md     # this file
├── requirements.txt
├── run.py                   # main loop
├── polymarket_client.py     # Gamma API + question parser
├── weather_forecast.py      # Open-Meteo ensemble + NOAA climate prior
├── strategy.py              # edge detection + Kelly sizing
├── polymarket_trader.py     # live-trading scaffold (optional)
├── dashboard.html           # auto-refresh dashboard
└── logs/
    └── .gitkeep             # placeholder; bot populates this folder
```
