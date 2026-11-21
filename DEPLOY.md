# Deploying TradeSetu on a VPS with Dokploy

What you get: the desk at `https://your-domain`, behind a sign-in, with Zerodha's
`kite-mcp-server` running privately beside it for live orders.

```
browser ──HTTPS──> Dokploy Traefik ──> desk :8000 ──private network──> kite-mcp :8080 ──> Zerodha
                                         │
                                         └── /data volume: settings, keys, paper book, algos, journal
```

Only `desk` is reachable from the internet. `kite-mcp` has no authentication of
its own, so it is never given a domain; the desk relays just its two Kite
sign-in pages under `/kite/`.

## 0. Before you start

| Need | Why |
|---|---|
| VPS with Dokploy installed, 2 GB RAM or more | The image build needs about 1.5 GB; running needs about 400 MB |
| A domain or subdomain, e.g. `desk.example.com` | HTTPS and the Kite redirect need a fixed name |
| This repository on GitHub | Dokploy builds from it |
| A Kite Connect app from <https://developers.kite.trade> | Its API key and secret place live orders |
| An LLM API key (OpenAI, Anthropic, Google, ...) | Only for the AI Analyze tab; backtests, charts and algos do not use it |

Zerodha allows one Kite Connect session per API key. SEBI's retail algo rules
require API orders to come from a static IP registered with the broker: register
your VPS's public IP in the Kite Connect developer console before trading live.

## 1. Point the domain at the VPS

At your DNS provider add an `A` record: `desk.example.com` → your VPS IP. Wait
until `ping desk.example.com` shows that IP.

## 2. Set up the Kite Connect app

In the Kite developer console, open your app and set:

- **Redirect URL**: `https://desk.example.com/kite/callback` (your domain, this exact path)
- Note the **API key** and **API secret**.

## 3. Create the service in Dokploy

1. **Projects → Create Project** (any name, e.g. `trading`).
2. Inside it: **Create Service → Compose**. Name it `tradesetu`. Compose type: **Docker Compose**.
3. **Provider**: GitHub → your fork of this repository, branch `main`.
   **Compose Path**: `./docker-compose.yml`. Save.

## 4. Environment

Open the service's **Environment** tab and paste, with your own values:

```env
DESK_DOMAIN=desk.example.com
DESK_ADMIN_USER=admin
DESK_ADMIN_PASSWORD=use-a-long-unique-password-here
KITE_API_KEY=your_kite_api_key
KITE_API_SECRET=your_kite_api_secret
```

- `DESK_DOMAIN` has no `https://` and no slash.
- `DESK_ADMIN_PASSWORD` must be at least 10 characters. The desk refuses to start
  without one. Anyone with it can place orders, so make it long and unique.
- Use only letters, digits, `-` and `_` in the password. Env files rewrite `$`
  (and what follows it) and cut the value at ` #`; quotes may become part of it.
  Length is what makes it strong: 20 or more characters.
- To keep the password itself out of the panel, set `DESK_ADMIN_PASSWORD_HASH`
  instead and leave `DESK_ADMIN_PASSWORD` empty. Get the hash from the running
  container: `docker exec -it <desk-container-name> python -m desk.auth`.
- Optional, `DESK_EXCHANGE_PROXY`: NSE and BSE refuse most VPS addresses ("403
  Forbidden" on Results Drift and Whale Deals). Set it to an HTTP proxy that
  exits from an Indian home or office connection, e.g.
  `DESK_EXCHANGE_PROXY=http://user:pass@100.64.0.5:8888`, and redeploy. Only NSE
  and BSE requests use it. Test from the VPS first:
  `curl -x http://user:pass@100.64.0.5:8888 -s -o /dev/null -w "%{http_code}\n" -H "Referer: https://www.bseindia.com/" "https://api.bseindia.com/BseIndiaAPI/api/Corpforthresults/w?fromdate=20261001&todate=20261120&scripcode="`
  should print `200`.
  The option history download (Backtest → Option strategy → Real option prices)
  uses the same proxy. Check NSE's archive through it with
  `curl -x http://user:pass@100.64.0.5:8888 -s -o /dev/null -w "%{http_code}\n" -A "Mozilla/5.0" "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20241101_F_0000.csv.zip"`
  (`200`). The history file, `option_history.db`, lives in the desk's data
  volume; 10 years of NIFTY, BANKNIFTY and FINNIFTY is about 700 MB.

## 5. Domain and HTTPS

Service → **Domains → Add Domain**:

| Field | Value |
|---|---|
| Service Name | `desk` |
| Host | `desk.example.com` |
| Path | `/` |
| Container Port | `8000` |
| HTTPS | on |
| Certificate | Let's Encrypt |

Do **not** add a domain for `kite-mcp`.

## 6. Deploy

Press **Deploy**. The first build takes 5 to 10 minutes (Python packages, and
the Go build of the Kite server). Watch **Deployments → logs**. When it is up:

- `desk` logs show `TradeSetu hosted on 0.0.0.0:8000, user 'admin'`.
- `https://desk.example.com` shows the sign-in page.

## 7. First run

1. Sign in with the user and password from step 4.
2. **Settings → AI model**: choose the provider, paste its API key, Save.
   (Skip if you only want backtests, charts and algos.)
3. **Trade → Connect**. The pill at the top should read **Kite: trading**.
4. **Log in to Kite** → open the link → sign in at Zerodha → you land on a
   "login successful" page → back in the desk press **Refresh**. Your funds and
   holdings appear.
5. Run a **Backtest**, then try a **paper** order and a **paper** algo.

Live trading stays off until you tick **Settings → Allow live orders**.

## Every trading day

Kite sessions end each morning (a Zerodha rule, not a setting). Before the
market: **Trade → Log in to Kite → Refresh**. A live algo that finds Kite signed
out skips that day and says so in its Activity log; it does not trade blind.

## Updating

Push to `main`, then press **Deploy** (or turn on **Auto Deploy** with the
GitHub webhook). Data in the `desk_data` volume survives redeploys. You sign in
to the desk and to Kite again after each deploy.

## Backup

Everything is in the Docker volume `desk_data` (settings with your API keys,
paper account, algos, order journal). Back it up from the VPS:

```bash
docker run --rm -v tradesetu_desk_data:/data -v "$PWD":/backup alpine tar czf /backup/desk-data.tgz -C /data .
```

The volume name is prefixed by the Dokploy app name; `docker volume ls | grep desk_data` shows the exact one.

## Without live trading

To run the desk alone (backtests, charts, AI analysis, paper trading), delete
the `kite-mcp` service, the `depends_on` block and the `DESK_KITE_MCP_URL` line
from `docker-compose.yml`, and drop the two `KITE_` variables.

## Troubleshooting

First look at the containers and the desk's log on the VPS:

```bash
docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -i -E 'desk|kite'
```

```bash
docker logs --tail 40 <desk-container-name>
```

A `desk` container that says `Restarting` is crashing at start, and the browser shows a blank or error page; the log names the reason.


| Symptom | Cause and fix |
|---|---|
| Deploy fails: `set DESK_ADMIN_PASSWORD` / `set DESK_DOMAIN` | A variable in step 4 is missing or empty |
| Blank page; `desk` is `Restarting`; log says `NumPy was built with baseline optimizations: (X86_V2)` | The VPS exposes an old virtual CPU. Fixed in the repo by pinning NumPy below 2.4: pull the latest `main` and redeploy. If you control the hypervisor, setting the VM's CPU type to `host` also fixes it |
| `desk` restarts with `Refusing to start` | Password missing or shorter than 10 characters |
| Browser shows `{"error":"forbidden host"}` | `DESK_DOMAIN` does not match the address in the browser |
| 404 or Traefik "no available server" | Domain's Service Name is not `desk`, or port is not `8000` |
| Sign-in works, then every click returns to sign-in | The site is on `http://`; turn HTTPS on for the domain |
| "Wrong username or password" with the right ones | The container received a different value than you typed in the panel, usually because of `$`, `#` or quotes in the password. Check with `docker exec -it <desk-container-name> python -m desk.auth check`, and see the reason in `docker logs <desk-container-name> \| grep -i sign-in`. Change the password to letters, digits, `-`, `_` and redeploy |
| Locked out of two-factor sign-in (phone and recovery codes lost) | Add `DESK_2FA_RESET=1` to the service's environment, redeploy, sign in with the password, set two-factor up again, then remove the variable and redeploy |
| Results Drift or Whale Deals: "403 Forbidden" | NSE/BSE block the VPS address. Set `DESK_EXCHANGE_PROXY` (step 4) and redeploy |
| "Too many failed sign-ins" | 5 wrong passwords lock that IP for 15 minutes. Redeploy to clear it sooner |
| Kite pill says "not connected" | Trade → Connect. Check `kite-mcp` logs: wrong API key/secret stops it |
| Kite login ends on an error page | Redirect URL in the Kite console is not exactly `https://<domain>/kite/callback` |
| Orders rejected by Kite for IP | Register the VPS IP in the Kite Connect console (SEBI static-IP rule) |
| Charts or backtests say "no price history" | Yahoo did not answer or does not carry the symbol; try the NSE symbol |
| Algo stuck on "Waiting for today's bar" | Exchange holiday, or Yahoo has not published the day's bar yet; it retries every 10 minutes until 23:30 IST |

## Running the same thing without Dokploy

```bash
cp .env.example .env   # fill it in
```

Add `ports: ["8000:8000"]` under `desk` in `docker-compose.yml`, then:

```bash
docker compose up -d --build
```

Put your own HTTPS reverse proxy in front. Do not expose it on plain HTTP: the
sign-in cookie is only marked Secure when the request arrived over HTTPS.
