# PEPEPOW Monitor

Lightweight, read-only PEPEPOW network/service monitoring using GitHub Actions and public APIs.

## What it monitors

- `explorer.pepepow.net/monitor/api/status`
  - chain height
  - network hashrate
  - last block age
  - monitor freshness / chain movement / persistent stall signals
- `light.pepepow.net/api/health` and `/api/status`
  - independent ElectrumX connectivity and chain height
- `explorer.pepepow.net`
  - public site availability
- `explorer.pepepow.org`
  - best-effort public site probe
  - Cloudflare currently challenges GitHub-hosted runners with HTTP 403; a recognized Cloudflare challenge is shown as `CF-BLOCKED` and is **not** treated as an outage
- `pool.pepepow.net` read-only API
  - health, pool summary, network summary, blocks and payments

The monitor cross-checks explorer height against PEPEW Light so a stale explorer node is not mistaken for a network-wide chain stall.

## Schedule

GitHub Actions runs at minute 7 and 37 of every hour (approximately every 30 minutes). GitHub scheduled workflows can occasionally start late.

Warnings normally require two consecutive failed runs. A chain stall is only treated as critical when independent evidence agrees. Normal latency and small height differences are ignored.

## Incident lifecycle

`NEW -> ACTIVE -> RECOVERED -> CLOSED`

- first warning observation: record only
- second consecutive warning observation: one alert email
- while active: do not repeat the same email
- severity escalation: alert again
- recovery: one recovery email

State is stored in a small GitHub Actions cache. The workflow keeps only the four newest monitor-state caches.

## Email setup

Monitoring works without email secrets, but alert emails remain pending until email delivery is configured.

Repository settings:

`Settings -> Secrets and variables -> Actions -> New repository secret`

Add:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM` (optional; defaults to `SMTP_USERNAME`)
- `ALERT_EMAIL_TO` (**required for alert delivery; keep the recipient address only in GitHub Actions Secrets**)

Example for a Gmail sender using an App Password:

- `SMTP_HOST = smtp.gmail.com`
- `SMTP_PORT = 465`
- `SMTP_USERNAME = your-gmail-address`
- `SMTP_PASSWORD = your-app-password`

Do not commit SMTP credentials, recipient email addresses, or other personal notification destinations into the repository.

## Manual test

Open **Actions -> PEPEPOW Health Monitor -> Run workflow**.

`dry_run` defaults to `true`, so a manual first run will exercise the monitor without sending email. Scheduled runs are live and will send alerts if the required email secrets are configured.

## Configuration

Edit `monitor/config.json`.

Defaults:

- check interval: 30 minutes
- warning persistence: 2 runs
- chain-stall last-block threshold: 1800 seconds
- possible payment stall: 3 hours
- abrupt hashrate drop: 80%
- HTTP timeout: 10 seconds
- retries: 3
- maximum JSON response: 20 MB

## Safety boundary

This repository is monitoring only. It does not:

- access daemon RPC or wallet RPC
- access SSH
- submit blocks or transactions
- send coins or trigger payouts
- restart or modify production services
- store wallet secrets or private keys

All monitored endpoints are public read-only endpoints.
