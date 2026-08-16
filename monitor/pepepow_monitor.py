#!/usr/bin/env python3
"""Read-only PEPEPOW public API health monitor for GitHub Actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

USER_AGENT = "PEPEPOW-GitHub-Monitor/2.0 (+https://github.com/edisontw/pepepow-monitor)"
TAIPEI = ZoneInfo("Asia/Taipei")
SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
STATE_VERSION = 2
MAX_RESPONSE_BYTES = 20_000_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def taipei_text(value: str | None = None) -> str:
    dt = parse_time(value) if value else utcnow()
    if dt is None:
        return value or "-"
    return dt.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S %Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    data: Any = None
    text: str = ""
    latency_ms: float | None = None
    error: str | None = None


def fetch(url: str, *, timeout: int, retries: int, expect_json: bool = True) -> FetchResult:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
    }
    last: FetchResult | None = None
    for attempt in range(max(1, retries)):
        started = time.perf_counter()
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                latency = round((time.perf_counter() - started) * 1000, 2)
                if len(raw) > MAX_RESPONSE_BYTES:
                    return FetchResult(url, False, status=status, latency_ms=latency, error="response_too_large")
                text = raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                if expect_json:
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError as exc:
                        return FetchResult(
                            url, False, status=status, text=text[:4000], latency_ms=latency,
                            error=f"invalid_json: {exc}",
                        )
                    return FetchResult(url, 200 <= status < 400, status=status, data=data, text=text[:4000], latency_ms=latency)
                lowered = text[:20000].lower()
                obvious_error = any(
                    token in lowered
                    for token in ("502 bad gateway", "503 service unavailable", "internal server error")
                )
                return FetchResult(
                    url, 200 <= status < 400 and not obvious_error, status=status,
                    text=text[:4000], latency_ms=latency,
                    error="obvious_error_page" if obvious_error else None,
                )
        except urllib.error.HTTPError as exc:
            latency = round((time.perf_counter() - started) * 1000, 2)
            try:
                body = exc.read(4000).decode("utf-8", errors="replace")
            except Exception:
                body = ""
            last = FetchResult(
                url, False, status=exc.code, text=body, latency_ms=latency,
                error=f"HTTP {exc.code}",
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            latency = round((time.perf_counter() - started) * 1000, 2)
            last = FetchResult(url, False, latency_ms=latency, error=f"{exc.__class__.__name__}: {exc}")
        if attempt + 1 < max(1, retries):
            time.sleep(min(4, 2 ** attempt))
    return last or FetchResult(url, False, error="request_failed")


def is_cloudflare_challenge(result: Any) -> bool:
    status = getattr(result, "status", None)
    text = str(getattr(result, "text", "") or "").lower()
    if status not in {403, 429, 503}:
        return False
    return any(marker in text for marker in ("just a moment", "cf-chl-", "cloudflare"))


def _get(mapping: Any, *keys: str, default: Any = None) -> Any:
    value = mapping
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def _to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "blocks", "payments", "data", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def stable_marker(payload: Any) -> str | None:
    items = _items(payload)
    if not items:
        return None
    encoded = json.dumps(items[:3], sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def light_height(payload: Any) -> int | None:
    return _to_int(_get(payload, "electrumx", "height"))


def light_ok(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get("ok")) and bool(
        _get(payload, "electrumx", "connected", default=False)
    )


def explorer_explicit_stall(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(_get(payload, "fork", "chain_moving_status", default="")).lower() == "stalled":
        return True
    for alert in payload.get("alerts") or []:
        if not isinstance(alert, dict):
            continue
        text = " ".join(str(alert.get(k, "")) for k in ("type", "title", "message")).lower()
        severity = str(alert.get("severity", "")).lower()
        if "stall" in text and severity in {"critical", "error"}:
            return True
    return False


def health_flag(payload: Any, key: str) -> bool:
    return isinstance(payload, dict) and bool(payload.get(key))


@dataclass
class Signal:
    name: str
    active: bool
    severity: str = "WARNING"
    immediate: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    url: str | None = None


def observe(config: dict[str, Any]) -> dict[str, Any]:
    timeout = int(config["http_timeout_seconds"])
    retries = int(config["http_retries"])
    s = config["sources"]
    results = {
        "explorer": fetch(s["explorer_status"], timeout=timeout, retries=retries),
        "light_health": fetch(s["light_health"], timeout=timeout, retries=retries),
        "light": fetch(s["light_status"], timeout=timeout, retries=retries),
        "explorer_net_site": fetch(s["explorer_net"], timeout=timeout, retries=retries, expect_json=False),
        "explorer_org_site": fetch(s["explorer_org"], timeout=timeout, retries=retries, expect_json=False),
        "pool_health": fetch(s["pool_health"], timeout=timeout, retries=retries),
        "pool_summary": fetch(s["pool_summary"], timeout=timeout, retries=retries),
        "pool_network": fetch(s["pool_network"], timeout=timeout, retries=retries),
        "pool_blocks": fetch(s["pool_blocks"], timeout=timeout, retries=retries),
        "pool_payments": fetch(s["pool_payments"], timeout=timeout, retries=retries),
    }
    explorer = results["explorer"].data if results["explorer"].ok else {}
    light = results["light"].data if results["light"].ok else {}
    pool_health = results["pool_health"].data if results["pool_health"].ok else {}
    pool_summary = results["pool_summary"].data if results["pool_summary"].ok else {}
    return {
        "checked_at": iso(),
        "results": results,
        "explorer_height": _to_int(explorer.get("height")) if isinstance(explorer, dict) else None,
        "explorer_hashrate": _to_float(explorer.get("hashrate_hps")) if isinstance(explorer, dict) else None,
        "last_block_age": _to_int(explorer.get("last_block_age")) if isinstance(explorer, dict) else None,
        "explorer_stale": bool(explorer.get("stale")) if isinstance(explorer, dict) else None,
        "explorer_freshness": _get(explorer, "freshness", "overall_status"),
        "explorer_chain_moving": _get(explorer, "fork", "chain_moving_status"),
        "explorer_explicit_stall": explorer_explicit_stall(explorer),
        "light_height": light_height(light),
        "light_ok": results["light"].ok and light_ok(light),
        "pool_stale": health_flag(pool_health, "stale"),
        "pool_degraded": health_flag(pool_health, "degraded"),
        "pool_status": str(pool_summary.get("poolStatus", "")).lower() if isinstance(pool_summary, dict) else "",
        "pool_block_marker": stable_marker(results["pool_blocks"].data) if results["pool_blocks"].ok else None,
        "payment_marker": stable_marker(results["pool_payments"].data) if results["pool_payments"].ok else None,
    }


def default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "last_run": None,
        "last_values": {},
        "tracking": {},
        "counters": {},
        "incidents": {},
    }


def update_change_tracking(state: dict[str, Any], obs: dict[str, Any], now: datetime) -> None:
    tracking = state.setdefault("tracking", {})
    previous = state.setdefault("last_values", {})
    for key, track_key in (
        ("pool_block_marker", "pool_block_last_change_at"),
        ("payment_marker", "payment_last_change_at"),
    ):
        current = obs.get(key)
        old = previous.get(key)
        if tracking.get(track_key) is None:
            tracking[track_key] = iso(now)
        elif current is not None and old is not None and current != old:
            tracking[track_key] = iso(now)
        elif old is None and current is not None:
            tracking[track_key] = iso(now)


def evaluate(
    obs: dict[str, Any],
    state: dict[str, Any],
    config: dict[str, Any],
    now: datetime | None = None,
) -> list[Signal]:
    now = now or utcnow()
    results: dict[str, FetchResult] = obs["results"]
    previous = state.get("last_values", {})
    signals: list[Signal] = []
    urls = config["sources"]

    explorer_ok = results["explorer"].ok
    light_api_ok = results["light"].ok and bool(obs.get("light_ok"))
    signals.append(Signal(
        "EXPLORER_API_DOWN", not explorer_ok,
        evidence={"status": getattr(results["explorer"], "status", None), "error": results["explorer"].error},
        url=urls["explorer_status"],
    ))
    signals.append(Signal(
        "LIGHT_API_DOWN", not light_api_ok,
        evidence={"http_ok": results["light"].ok, "status": getattr(results["light"], "status", None),
                  "error": results["light"].error, "light_ok": obs.get("light_ok")},
        url=urls["light_status"],
    ))

    prev_explorer = _to_int(previous.get("explorer_height"))
    prev_light = _to_int(previous.get("light_height"))
    cur_explorer = _to_int(obs.get("explorer_height"))
    cur_light = _to_int(obs.get("light_height"))
    explorer_moved = None if prev_explorer is None or cur_explorer is None else cur_explorer > prev_explorer
    light_moved = None if prev_light is None or cur_light is None else cur_light > prev_light

    signals.append(Signal(
        "EXPLORER_NODE_STALE",
        explorer_ok and light_api_ok and explorer_moved is False and light_moved is True,
        evidence={"previous_explorer_height": prev_explorer, "explorer_height": cur_explorer,
                  "previous_light_height": prev_light, "light_height": cur_light},
        url=urls["explorer_status"],
    ))
    signals.append(Signal(
        "LIGHT_NODE_STALE",
        explorer_ok and light_api_ok and light_moved is False and explorer_moved is True,
        evidence={"previous_explorer_height": prev_explorer, "explorer_height": cur_explorer,
                  "previous_light_height": prev_light, "light_height": cur_light},
        url=urls["light_status"],
    ))

    last_age = _to_int(obs.get("last_block_age"))
    both_not_moving = explorer_moved is False and light_moved is False
    confirmed_stall = bool(
        explorer_ok and light_api_ok and both_not_moving
        and (
            obs.get("explorer_explicit_stall")
            or (last_age is not None and last_age >= int(config["chain_stall_seconds"]))
        )
    )
    signals.append(Signal(
        "NETWORK_CHAIN_STALL", confirmed_stall, severity="CRITICAL",
        immediate=bool(obs.get("explorer_explicit_stall")),
        evidence={"explorer_height": cur_explorer, "light_height": cur_light,
                  "last_block_age_seconds": last_age, "explorer_chain_moving": obs.get("explorer_chain_moving")},
        url=urls["explorer_status"],
    ))
    signals.append(Signal(
        "NETWORK_STATUS_UNKNOWN",
        (not explorer_ok or cur_explorer is None) and (not light_api_ok or cur_light is None),
        evidence={"explorer_api_ok": explorer_ok, "light_api_ok": light_api_ok},
        url=urls["explorer_status"],
    ))

    net_result = results["explorer_net_site"]
    org_result = results["explorer_org_site"]
    net_site_ok = net_result.ok
    org_site_ok = org_result.ok
    org_cf_blocked = is_cloudflare_challenge(org_result)

    if not net_site_ok and not org_site_ok and not org_cf_blocked:
        signals.append(Signal(
            "PUBLIC_EXPLORERS_DOWN", True,
            evidence={"explorer_net_error": net_result.error, "explorer_org_error": org_result.error},
            url=urls["explorer_net"],
        ))
        signals.append(Signal("EXPLORER_NET_SITE_DOWN", False))
        signals.append(Signal("EXPLORER_ORG_SITE_DOWN", False))
    else:
        signals.append(Signal("PUBLIC_EXPLORERS_DOWN", False))
        signals.append(Signal(
            "EXPLORER_NET_SITE_DOWN", not net_site_ok,
            evidence={"status": getattr(net_result, "status", None), "error": net_result.error},
            url=urls["explorer_net"],
        ))
        # GitHub-hosted runners are challenged by Cloudflare on pepepow.org.
        # That is "not observable from this runner", not evidence of an outage.
        signals.append(Signal(
            "EXPLORER_ORG_SITE_DOWN", (not org_site_ok) and (not org_cf_blocked),
            evidence={"status": getattr(org_result, "status", None), "error": org_result.error},
            url=urls["explorer_org"],
        ))

    pool_core_keys = ("pool_health", "pool_summary", "pool_network")
    pool_core_failures = [key for key in pool_core_keys if not results[key].ok]
    signals.append(Signal(
        "POOL_API_DOWN", len(pool_core_failures) >= 2,
        evidence={"failed_endpoints": pool_core_failures},
        url=urls["pool_health"],
    ))
    signals.append(Signal(
        "POOL_API_DEGRADED", 0 < len(pool_core_failures) < 2,
        evidence={"failed_endpoints": pool_core_failures},
        url=urls["pool_health"],
    ))
    signals.append(Signal(
        "POOL_DATA_STALE",
        bool(results["pool_health"].ok and (obs.get("pool_stale") or obs.get("pool_degraded"))),
        evidence={"stale": obs.get("pool_stale"), "degraded": obs.get("pool_degraded")},
        url=urls["pool_health"],
    ))
    signals.append(Signal(
        "POOL_SERVICE_DOWN",
        bool(results["pool_summary"].ok and obs.get("pool_status") in {"down", "offline", "error", "stopped", "failed"}),
        evidence={"pool_status": obs.get("pool_status")},
        url=urls["pool_summary"],
    ))
    signals.append(Signal(
        "PAYMENTS_API_DOWN", not results["pool_payments"].ok,
        evidence={"status": getattr(results["pool_payments"], "status", None),
                  "error": results["pool_payments"].error},
        url=urls["pool_payments"],
    ))

    tracking = state.get("tracking", {})
    payment_last_changed = parse_time(tracking.get("payment_last_change_at"))
    block_last_changed = parse_time(tracking.get("pool_block_last_change_at"))
    payment_stale = bool(
        results["pool_payments"].ok
        and results["pool_blocks"].ok
        and payment_last_changed
        and block_last_changed
        and block_last_changed > payment_last_changed
        and (now - payment_last_changed).total_seconds() >= float(config["payment_stale_hours"]) * 3600
    )
    signals.append(Signal(
        "POSSIBLE_PAYMENT_STALL", payment_stale,
        evidence={"payment_last_change_at": tracking.get("payment_last_change_at"),
                  "pool_block_last_change_at": tracking.get("pool_block_last_change_at"),
                  "threshold_hours": config["payment_stale_hours"]},
        url=urls["pool_payments"],
    ))

    current_hashrate = _to_float(obs.get("explorer_hashrate"))
    previous_hashrate = _to_float(previous.get("explorer_hashrate"))
    drop_fraction = float(config["hashrate_drop_percent"]) / 100.0
    hashrate_bad = False
    if explorer_ok and current_hashrate is not None:
        if current_hashrate <= 0:
            hashrate_bad = True
        elif previous_hashrate and previous_hashrate > 0:
            hashrate_bad = current_hashrate <= previous_hashrate * (1.0 - drop_fraction)
    signals.append(Signal(
        "NETWORK_HASHRATE_COLLAPSE", hashrate_bad,
        evidence={"previous_hashrate_hps": previous_hashrate, "hashrate_hps": current_hashrate,
                  "drop_threshold_percent": config["hashrate_drop_percent"]},
        url=urls["explorer_status"],
    ))
    return signals


def process_incidents(
    state: dict[str, Any],
    signals: list[Signal],
    config: dict[str, Any],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or utcnow()
    incidents = state.setdefault("incidents", {})
    counters = state.setdefault("counters", {})
    required = int(config["consecutive_failures"])
    events: list[dict[str, Any]] = []
    by_name = {signal.name: signal for signal in signals}

    for name in sorted(set(by_name) | set(incidents)):
        signal = by_name.get(name, Signal(name, False))
        incident = incidents.get(name)
        if signal.active:
            counters[name] = int(counters.get(name, 0)) + 1
            if signal.immediate or counters[name] >= required:
                if not incident or incident.get("status") in {"CLOSED", "RECOVERED"}:
                    incident = {
                        "status": "ACTIVE",
                        "severity": signal.severity,
                        "opened_at": iso(now),
                        "last_seen_at": iso(now),
                        "evidence": signal.evidence,
                        "url": signal.url,
                        "alert_notified": False,
                        "recovery_notified": False,
                    }
                    incidents[name] = incident
                else:
                    incident["last_seen_at"] = iso(now)
                    incident["evidence"] = signal.evidence
                    incident["url"] = signal.url or incident.get("url")
                    if SEVERITY_ORDER.get(signal.severity, 1) > SEVERITY_ORDER.get(str(incident.get("severity")), 1):
                        incident["severity"] = signal.severity
                        incident["alert_notified"] = False
                if not incident.get("alert_notified"):
                    events.append({"type": "ALERT", "name": name, **incident})
        else:
            counters[name] = 0
            if incident and incident.get("status") == "ACTIVE":
                incident["status"] = "RECOVERED"
                incident["recovered_at"] = iso(now)
                incident["recovery_notified"] = False
            if incident and incident.get("status") == "RECOVERED" and not incident.get("recovery_notified"):
                events.append({"type": "RECOVERY", "name": name, **incident})
    return events


def mark_event_notified(state: dict[str, Any], event: dict[str, Any]) -> None:
    incident = state.get("incidents", {}).get(event["name"])
    if not incident:
        return
    if event["type"] == "ALERT":
        incident["alert_notified"] = True
        incident["last_notified_at"] = iso()
    elif event["type"] == "RECOVERY":
        incident["recovery_notified"] = True
        incident["status"] = "CLOSED"
        incident["closed_at"] = iso()


def snapshot_last_values(state: dict[str, Any], obs: dict[str, Any]) -> None:
    state["last_run"] = obs.get("checked_at")
    state["last_values"] = {
        "explorer_height": obs.get("explorer_height"),
        "light_height": obs.get("light_height"),
        "explorer_hashrate": obs.get("explorer_hashrate"),
        "pool_block_marker": obs.get("pool_block_marker"),
        "payment_marker": obs.get("payment_marker"),
    }


def event_email(event: dict[str, Any], obs: dict[str, Any]) -> tuple[str, str]:
    name = event["name"]
    if event["type"] == "RECOVERY":
        return (
            f"[PEPEPOW RECOVERED] {name}",
            "\n".join([
                "PEPEPOW 監控已確認服務恢復。",
                "",
                f"事件：{name}",
                f"恢復時間：{taipei_text(event.get('recovered_at'))}",
                f"先前嚴重度：{event.get('severity', '-')}",
                f"事件開始：{taipei_text(event.get('opened_at'))}",
                f"目前 Explorer height：{obs.get('explorer_height')}",
                f"目前 Light height：{obs.get('light_height')}",
            ]),
        )
    evidence = json.dumps(event.get("evidence") or {}, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        f"[PEPEPOW ALERT] {event.get('severity', 'WARNING')} - {name}",
        "\n".join([
            "PEPEPOW 自動監控偵測到持續性異常。",
            "",
            f"時間：{taipei_text(event.get('last_seen_at'))}",
            f"Severity：{event.get('severity', 'WARNING')}",
            f"事件：{name}",
            f"持續起點：{taipei_text(event.get('opened_at'))}",
            "",
            "Observed:",
            evidence,
            "",
            f"Explorer height：{obs.get('explorer_height')}",
            f"Light height：{obs.get('light_height')}",
            f"Network hashrate：{obs.get('explorer_hashrate')}",
            f"Last block age：{obs.get('last_block_age')} s",
            "",
            f"相關 URL：{event.get('url') or '-'}",
            "",
            "Possible cause: 未自動判定。請依上述 observed evidence 人工檢查。",
        ]),
    )


def send_email(subject: str, body: str, recipient: str, *, dry_run: bool) -> tuple[bool, str]:
    if dry_run:
        return False, f"dry-run: {subject}"
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", "").strip() or username
    port = int(os.getenv("SMTP_PORT", "465") or 465)
    if not host or not username or not password or not sender:
        return False, "SMTP secrets missing; alert remains pending"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)
    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as server:
                server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(username, password)
                server.send_message(msg)
        return True, "sent"
    except Exception as exc:
        return False, f"SMTP send failed: {exc.__class__.__name__}: {exc}"


def summary_markdown(obs: dict[str, Any], state: dict[str, Any], email_notes: list[str]) -> str:
    r: dict[str, FetchResult] = obs["results"]
    active = [name for name, inc in state.get("incidents", {}).items() if inc.get("status") == "ACTIVE"]
    org_status = "OK" if r["explorer_org_site"].ok else (
        "CF-BLOCKED (not alerting)" if is_cloudflare_challenge(r["explorer_org_site"]) else "FAIL"
    )
    lines = [
        "# PEPEPOW Monitor",
        "",
        f"Checked: {taipei_text(obs.get('checked_at'))}",
        "",
        "| Component | Status |",
        "|---|---|",
        f"| Explorer monitor API | {'OK' if r['explorer'].ok else 'FAIL'} |",
        f"| Light / ElectrumX | {'OK' if obs.get('light_ok') else 'FAIL'} |",
        f"| explorer.pepepow.net | {'OK' if r['explorer_net_site'].ok else 'FAIL'} |",
        f"| explorer.pepepow.org | {org_status} |",
        f"| Pool API | {'OK' if r['pool_health'].ok else 'FAIL'} |",
        f"| Payments API | {'OK' if r['pool_payments'].ok else 'FAIL'} |",
        "",
        f"- Explorer height: `{obs.get('explorer_height')}`",
        f"- Light height: `{obs.get('light_height')}`",
        f"- Height difference: `{None if obs.get('explorer_height') is None or obs.get('light_height') is None else obs.get('explorer_height') - obs.get('light_height')}`",
        f"- Network hashrate: `{obs.get('explorer_hashrate')}` H/s",
        f"- Last block age: `{obs.get('last_block_age')}` s",
        f"- Open incidents: `{len(active)}`" + (f" — {', '.join(active)}" if active else ""),
    ]
    failures = [
        (key, value) for key, value in r.items()
        if not value.ok and not (key == "explorer_org_site" and is_cloudflare_challenge(value))
    ]
    if failures:
        lines += ["", "## Failed probes"]
        for key, value in failures:
            lines.append(f"- {key}: status={value.status}, error={value.error}")
    if email_notes:
        lines += ["", "## Notification", *[f"- {note}" for note in email_notes]]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="monitor/config.json")
    parser.add_argument("--state", default=".monitor-state/state.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_json(Path(args.config), {})
    if not config:
        print("Configuration missing or invalid", file=sys.stderr)
        return 2
    state = load_json(Path(args.state), default_state())
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        # State schema v2 intentionally clears the early diagnostic false incidents.
        state = default_state()

    now = utcnow()
    obs = observe(config)
    update_change_tracking(state, obs, now)
    events = process_incidents(state, evaluate(obs, state, config, now), config, now)

    recipient = os.getenv("ALERT_EMAIL_TO", "").strip() or config.get("alert_email_to", "")
    email_notes: list[str] = []
    for event in events:
        subject, body = event_email(event, obs)
        sent, note = send_email(subject, body, recipient, dry_run=args.dry_run)
        email_notes.append(note)
        if sent:
            mark_event_notified(state, event)

    snapshot_last_values(state, obs)
    save_json(Path(args.state), state)
    summary = summary_markdown(obs, state, email_notes)
    print(summary)
    if path := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
