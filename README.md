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
- second consecutive warning observation: create one GitHub Issue
- while active: do not create duplicate Issues
- severity escalation: add an update comment to the existing Issue
- recovery: add a `RECOVERED` comment and automatically close the Issue

State is stored in a small GitHub Actions cache. The workflow keeps only the four newest monitor-state caches.

## Notifications

The monitor uses GitHub Issues as the incident notification channel. SMTP and recipient email addresses are not used or stored by this repository.

When a confirmed incident is opened:

1. `github-actions[bot]` creates an Issue such as `[PEPEPOW ALERT] WARNING - EXPLORER_NODE_STALE`.
2. The Issue is assigned to the repository owner to make GitHub notification delivery more reliable.
3. GitHub can deliver the Issue notification through GitHub Inbox and, depending on the account notification settings, email.
4. When the service recovers, the monitor posts a recovery comment and closes the Issue.

To receive email notifications, configure the desired email address in the GitHub account notification settings. No recipient address needs to be added to this repository or to Actions Secrets.

Separately, GitHub Actions workflow-failure notifications can be enabled so failures of the monitoring system itself are also reported.

## Manual test

Open **Actions -> PEPEPOW Health Monitor -> Run workflow**.

`dry_run` defaults to `true`, so a manual test runs all health checks and incident logic without creating, updating, or closing GitHub Issues. Scheduled runs are live.

## GitHub permissions

The workflow has only the permissions required for this design:

- `contents: read`
- `actions: write` for the small state cache cleanup
- `issues: write` for alert/recovery Issues

No additional notification credentials are required.

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

## Public incident data

GitHub Issues are public because this repository is public. Incident Issues should contain only public monitoring information such as:

- timestamps
- public chain heights
- public network hashrate
- public API/service status
- public endpoint URLs

Do not add email addresses, private IP addresses, credentials, wallet data, private RPC information, server paths, or other sensitive operational data to Issues.

## Safety boundary

This repository is monitoring only. It does not:

- access daemon RPC or wallet RPC
- access SSH
- submit blocks or transactions
- send coins or trigger payouts
- restart or modify production services
- store wallet secrets or private keys
- store SMTP credentials or recipient email addresses

All monitored endpoints are public read-only endpoints.
