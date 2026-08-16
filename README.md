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
- `pepepow.org`
  - independent best-effort website probe managed separately from the explorer checks
  - checks public HTML availability when GitHub-hosted runners can reach it
  - when HTML is readable, samples up to 12 homepage / lazy-loaded / OG / Twitter image URLs
  - same-site image HTTP/HTML failures can become `PEPEPOW_ORG_IMAGES_BROKEN` after two consecutive runs
  - external-image failures are informational only to reduce false positives from third-party hotlink protection
  - if Cloudflare challenges the GitHub runner, status is `CF-BLOCKED` and no outage Issue is opened
- `pool.pepepow.net` read-only API
  - health, pool summary, network summary, blocks and payments
- PEPEPOW Stratum / external pools
  - Foztor: `stratum-eu.pepepow.foztor.net:13232`
  - zpool: `hoohash-pepew.eu.mine.zpool.ca:8335`
  - PEPEPOW PPLNS: `pool.pepepow.net:39333`
  - PEPEPOW SOLO: `pool.pepepow.net:39334`
  - Bowserlab: `bowserlab.ddns.net:9912`
  - each Stratum endpoint is checked with a short TCP connection only; no mining login or share submission is performed
  - Foztor is additionally checked through `https://pepepow.foztor.net/api/stats`
  - zpool is checked through `/api/status` and `/api/currencies`
  - Bowserlab is checked through `/api/status` and `/api/currencies`
  - missing PEPEW/Hoohash entries, port mismatches, API failures, and explicit zpool PEPEW API errors can trigger incidents
  - zero workers, zero pool hashrate, and long time since a block do **not** by themselves trigger alerts

The monitor cross-checks explorer height against PEPEW Light so a stale explorer node is not mistaken for a network-wide chain stall.

## Schedule

GitHub Actions runs at minute 7 and 37 of every hour (approximately every 30 minutes). GitHub scheduled workflows can occasionally start late.

Warnings normally require two consecutive failed runs. A chain stall is only treated as critical when independent evidence agrees. Normal latency and small height differences are ignored.

External-pool incidents also require two consecutive runs. Examples include `FOZTOR_STRATUM_DOWN`, `ZPOOL_API_DOWN`, `PEPEPOW_SOLO_STRATUM_DOWN`, `BOWSERLAB_API_DOWN`, and `ZPOOL_PEPEW_API_WARNING`.

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

`dry_run` defaults to `true`, so the main network/service monitor runs without creating, updating, or closing GitHub Issues. The external-pool and `pepepow.org` probes are skipped in this default dry run so their persistent incident state is not changed. Set `dry_run` to `false` only when an actual live notification test is intended. Scheduled runs are live.

## GitHub permissions

The workflow has only the permissions required for this design:

- `contents: read`
- `actions: write` for the small state cache cleanup
- `issues: write` for alert/recovery Issues

No additional notification credentials are required.

## Configuration

Edit `monitor/config.json` for the network/service monitor.

Defaults:

- check interval: 30 minutes
- warning persistence: 2 runs
- chain-stall last-block threshold: 1800 seconds
- possible payment stall: 3 hours
- abrupt hashrate drop: 80%
- HTTP timeout: 10 seconds
- retries: 3
- maximum JSON response: 20 MB

The `pepepow.org` probe is intentionally small and independent. Its current defaults are two consecutive failures, a 12-second HTTP timeout, and at most 12 sampled image URLs per run.

The external-pool probe is also independent. It uses two TCP attempts with a short timeout and requires two consecutive failed scheduled runs before opening a normal incident Issue.

## Public incident data

GitHub Issues are public because this repository is public. Incident Issues should contain only public monitoring information such as:

- timestamps
- public chain heights
- public network hashrate
- public API/service status
- public endpoint URLs
- public website/image HTTP status
- public pool/Stratum hostname and port status

Do not add email addresses, private IP addresses, credentials, wallet data, private RPC information, server paths, or other sensitive operational data to Issues.

## Safety boundary

This repository is monitoring only. It does not:

- access daemon RPC or wallet RPC
- access SSH
- submit blocks or transactions
- authenticate to Stratum pools
- submit mining shares
- send coins or trigger payouts
- restart or modify production services
- store wallet secrets or private keys
- store SMTP credentials or recipient email addresses

All monitored endpoints are public read-only endpoints or public TCP service ports.
