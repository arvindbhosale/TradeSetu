# Security Policy

TradeSetu can place real orders on your broker account, so security issues matter more here than in most projects.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report it privately through GitHub: **Security → Report a vulnerability** on this repository
(<https://github.com/arvindbhosale/TradeSetu/security/advisories/new>).

Include what you found, how to reproduce it, and what an attacker could do with it. You will get a reply within 7 days. Once a fix is released, you will be credited in the advisory unless you prefer not to be.

### In scope

- Bypassing the desk sign-in, two-factor sign-in or passkeys
- Placing, changing or cancelling orders without the account owner's action
- Leaking Kite API keys, access tokens, LLM keys or the admin password (in logs, API responses, exports or the browser)
- Cross-site scripting, request forgery or host-header attacks on the hosted desk
- Reaching the private `kite-mcp` service from outside the Docker network

### Out of scope

- Losses from strategies, backtests, signals or bugs in trading logic. Report those as normal issues.
- Problems in Zerodha Kite, `kite-mcp-server`, Dokploy or an LLM provider. Report those to them.
- Attacks that need someone who already controls your server or computer.

## Supported versions

Only the latest commit on `main` gets security fixes.

## Keeping your own setup safe

These are your responsibility when you run TradeSetu:

1. **Never commit secrets.** Keep keys in `.env` (git-ignored) or the desk's Settings, never in code, issues or screenshots. If a key leaks, regenerate the Kite API secret in the Kite developer console straight away.
2. **Use HTTPS and a strong admin password** in hosted mode, and turn on two-factor sign-in or passkeys under Settings.
3. **Do not expose `kite-mcp`.** It has no sign-in of its own. Never give it a domain or publish its port.
4. **Register a static IP.** SEBI rules require Kite API orders to come from a static IP registered in your Kite Connect app. Only your server's IP should be registered.
5. **Keep "Allow live orders" off** until you have tested a strategy on paper, and set the risk limits (maximum order value, daily loss limit, kill switch) under Settings.
6. **Update regularly** with `git pull`, then rebuild or reinstall.
7. **Back up the data folder** (`TRADESETU_HOME`, or the `/data` volume in Docker). It holds your settings and keys, so store the backup encrypted.
