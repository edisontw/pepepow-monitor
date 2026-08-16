#!/usr/bin/env python3
"""Best-effort external PEPEPOW pool/Stratum monitor for GitHub Actions."""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from pepepow_monitor import fetch, github_api, iso, load_json, save_json, taipei_text

STATE_VERSION = 1
CONSECUTIVE_FAILURES = 2
TCP_TIMEOUT_SECONDS = 6
TCP_ATTEMPTS = 2

POOLS = {
    "FOZTOR": {
        "label": "Foztor",
        "host": "stratum-eu.pepepow.foztor.net",
        "port": 13232,
        "api": "https://pepepow.foztor.net/api/stats",
    },
    "ZPOOL": {
        "label": "zpool",
        "host": "hoohash-pepew.eu.mine.zpool.ca",
        "port": 8335,
        "status_api": "https://www.zpool.ca/api/status",
        "currencies_api": "https://www.zpool.ca/api/currencies",
    },
    "PEPEPOW_PPLNS": {
        "label": "pool.pepepow.net PPLNS",
        "host": "pool.pepepow.net",
        "port": 39333,
    },
    "PEPEPOW_SOLO": {
        "label": "pool.pepepow.net SOLO",
        "host": "pool.pepepow.net",
        "port": 39334,
    },
    "BOWSERLAB": {
        "label": "Bowserlab",
        "host": "bowserlab.ddns.net",
        "port": 9912,
        "status_api": "https://bowserlab.ddns.net/api/status",
        "currencies_api": "https://bowserlab.ddns.net/api/currencies",
    },
}


def tcp_probe(host: str, port: int, *, timeout: int = TCP_TIMEOUT_SECONDS, attempts: int = TCP_ATTEMPTS) -> dict[str, Any]:
    errors: list[str] = []
    for attempt in range(max(1, attempts)):
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                latency = round((time.perf_counter() - started) * 1000, 2)
                return {"ok": True, "latency_ms": latency, "error": None}
        except OSError as exc:
            errors.append(f"{exc.__class__.__name__}: {exc}")
            if attempt + 1 < attempts:
                time.sleep(1)
    return {"ok": False, "latency_ms": None, "error": errors[-1] if errors else "connection_failed"}


def foztor_health(payload: Any) -> dict[str, Any]:
    pool = payload.get("pools", {}).get("hoohashv110-pepew") if isinstance(payload, dict) else None
    healthy = isinstance(pool, dict) and str(pool.get("symbol", "")).upper() == "PEPEW"
    return {
        "ok": healthy,
        "workers": pool.get("workerCount") if isinstance(pool, dict) else None,
        "hashrate": pool.get("hashrate") if isinstance(pool, dict) else None,
        "algorithm": pool.get("algorithm") if isinstance(pool, dict) else None,
        "error": None if healthy else "PEPEW pool entry missing or invalid",
    }


def yiimp_health(status_payload: Any, currencies_payload: Any, *, expected_port: int) -> dict[str, Any]:
    algo = status_payload.get("hoohash-pepew") if isinstance(status_payload, dict) else None
    coin = currencies_payload.get("PEPEW") if isinstance(currencies_payload, dict) else None
    algo_port = None
    coin_port = None
    try:
        algo_port = int(algo.get("port")) if isinstance(algo, dict) and algo.get("port") is not None else None
    except (TypeError, ValueError):
        pass
    try:
        coin_port = int(coin.get("port")) if isinstance(coin, dict) and coin.get("port") is not None else None
    except (TypeError, ValueError):
        pass
    healthy = isinstance(algo, dict) and isinstance(coin, dict) and algo_port == expected_port and coin_port == expected_port
    warning = str(coin.get("error", "") or "").strip() if isinstance(coin, dict) else ""
    return {
        "ok": healthy,
        "workers": algo.get("workers") if isinstance(algo, dict) else None,
        "hashrate": algo.get("hashrate") if isinstance(algo, dict) else None,
        "height": coin.get("height") if isinstance(coin, dict) else None,
        "lastblock": coin.get("lastblock") if isinstance(coin, dict) else None,
        "timesincelast": coin.get("timesincelast") if isinstance(coin, dict) else None,
        "warning": warning,
        "error": None if healthy else f"PEPEW/hoohash-pepew entry missing or port mismatch (expected {expected_port})",
    }


def observe() -> dict[str, Any]:
    tcp = {key: tcp_probe(cfg["host"], int(cfg["port"])) for key, cfg in POOLS.items()}

    foztor_result = fetch(POOLS["FOZTOR"]["api"], timeout=12, retries=2)
    foztor = foztor_health(foztor_result.data) if foztor_result.ok else {
        "ok": False, "workers": None, "hashrate": None, "algorithm": None,
        "error": foztor_result.error or f"HTTP {foztor_result.status}",
    }

    z_status = fetch(POOLS["ZPOOL"]["status_api"], timeout=15, retries=2)
    z_currencies = fetch(POOLS["ZPOOL"]["currencies_api"], timeout=20, retries=2)
    if z_status.ok and z_currencies.ok:
        zpool = yiimp_health(z_status.data, z_currencies.data, expected_port=8335)
    else:
        zpool = {
            "ok": False, "workers": None, "hashrate": None, "height": None, "lastblock": None,
            "timesincelast": None, "warning": "",
            "error": z_status.error or z_currencies.error or "zpool API unavailable",
        }

    b_status = fetch(POOLS["BOWSERLAB"]["status_api"], timeout=15, retries=2)
    b_currencies = fetch(POOLS["BOWSERLAB"]["currencies_api"], timeout=20, retries=2)
    if b_status.ok and b_currencies.ok:
        bowser = yiimp_health(b_status.data, b_currencies.data, expected_port=9912)
    else:
        bowser = {
            "ok": False, "workers": None, "hashrate": None, "height": None, "lastblock": None,
            "timesincelast": None, "warning": "",
            "error": b_status.error or b_currencies.error or "Bowserlab API unavailable",
        }

    return {
        "checked_at": iso(),
        "tcp": tcp,
        "api": {"FOZTOR": foztor, "ZPOOL": zpool, "BOWSERLAB": bowser},
    }


def default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "counters": {}, "incidents": {}}


def signals(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, cfg in POOLS.items():
        tcp = obs["tcp"][key]
        out[f"{key}_STRATUM_DOWN"] = {
            "active": not tcp["ok"],
            "label": cfg["label"],
            "evidence": {"host": cfg["host"], "port": cfg["port"], "tcp_error": tcp.get("error")},
        }
    for key in ("FOZTOR", "ZPOOL", "BOWSERLAB"):
        api = obs["api"][key]
        out[f"{key}_API_DOWN"] = {
            "active": not api["ok"],
            "label": POOLS[key]["label"],
            "evidence": api,
        }
    zwarning = str(obs["api"]["ZPOOL"].get("warning", "") or "").strip()
    out["ZPOOL_PEPEW_API_WARNING"] = {
        "active": bool(zwarning),
        "label": "zpool PEPEW",
        "evidence": {"warning": zwarning, "height": obs["api"]["ZPOOL"].get("height")},
    }
    return out


def process(state: dict[str, Any], current: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    counters = state.setdefault("counters", {})
    incidents = state.setdefault("incidents", {})
    events: list[dict[str, Any]] = []
    for name in sorted(set(current) | set(incidents)):
        sig = current.get(name, {"active": False, "label": name, "evidence": {}})
        incident = incidents.get(name)
        if sig["active"]:
            counters[name] = int(counters.get(name, 0)) + 1
            if counters[name] >= CONSECUTIVE_FAILURES:
                if not incident or incident.get("status") == "CLOSED":
                    incident = {
                        "status": "ACTIVE",
                        "opened_at": iso(),
                        "last_seen_at": iso(),
                        "label": sig.get("label", name),
                        "evidence": sig.get("evidence", {}),
                        "issue_number": None,
                        "notified": False,
                    }
                    incidents[name] = incident
                else:
                    incident["last_seen_at"] = iso()
                    incident["evidence"] = sig.get("evidence", {})
                if not incident.get("notified"):
                    events.append({"type": "ALERT", "name": name, **incident})
        else:
            counters[name] = 0
            if incident and incident.get("status") == "ACTIVE":
                incident["status"] = "RECOVERED"
                incident["recovered_at"] = iso()
                events.append({"type": "RECOVERY", "name": name, **incident})
    return events


def find_open_issue(title: str) -> int | None:
    ok, result, _ = github_api("GET", "issues?state=open&per_page=100")
    if not ok or not isinstance(result, list):
        return None
    for item in result:
        if isinstance(item, dict) and item.get("title") == title and "pull_request" not in item:
            try:
                return int(item["number"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def notify(event: dict[str, Any], *, dry_run: bool) -> tuple[bool, int | None, str]:
    name = event["name"]
    issue_number = event.get("issue_number")
    title = f"[PEPEPOW ALERT] WARNING - {name}"
    if dry_run:
        return False, None, f"dry-run: {event['type']} {name}"

    if event["type"] == "ALERT":
        existing = int(issue_number) if issue_number else find_open_issue(title)
        if existing:
            return True, existing, f"Issue #{existing} already open"
        evidence = json.dumps(event.get("evidence") or {}, ensure_ascii=False, indent=2, sort_keys=True)
        body = "\n".join([
            "PEPEPOW 外部礦池監控偵測到持續性異常。",
            "",
            f"- Pool：**{event.get('label', name)}**",
            f"- Event：`{name}`",
            f"- First confirmed：{taipei_text(event.get('opened_at'))}",
            f"- Last seen：{taipei_text(event.get('last_seen_at'))}",
            "",
            "### Observed",
            "```json",
            evidence,
            "```",
            "",
            "> 只使用公開 API 與 TCP reachability；0 workers / 0 hashrate 本身不視為故障。",
            "",
            "<!-- pepepow-external-pool-monitor -->",
        ])
        repo = os.getenv("GITHUB_REPOSITORY", "")
        owner = repo.split("/", 1)[0] if "/" in repo else ""
        payload: dict[str, Any] = {"title": title, "body": body}
        if owner:
            payload["assignees"] = [owner]
        ok, result, note = github_api("POST", "issues", payload)
        if not ok or not isinstance(result, dict):
            return False, None, note
        try:
            number = int(result["number"])
        except (KeyError, TypeError, ValueError):
            return False, None, "Issue created but number missing"
        return True, number, f"Issue #{number} created"

    if not issue_number:
        return True, None, "Recovered without an Issue"
    number = int(issue_number)
    comment = "\n".join([
        "## RECOVERED",
        "",
        f"此事件已於 {taipei_text(event.get('recovered_at'))} 恢復。",
        "",
        "Issue 由 PEPEPOW monitor 自動關閉。",
    ])
    ok, _, note = github_api("POST", f"issues/{number}/comments", {"body": comment})
    if not ok:
        return False, number, note
    ok, _, note = github_api("PATCH", f"issues/{number}", {"state": "closed", "state_reason": "completed"})
    return ok, number, f"Issue #{number} recovered and closed" if ok else note


def mark(state: dict[str, Any], event: dict[str, Any], issue_number: int | None) -> None:
    inc = state.get("incidents", {}).get(event["name"])
    if not inc:
        return
    if issue_number is not None:
        inc["issue_number"] = issue_number
    if event["type"] == "ALERT":
        inc["notified"] = True
    else:
        inc["status"] = "CLOSED"
        inc["closed_at"] = iso()
        inc["notified"] = True


def status_text(obs: dict[str, Any], key: str) -> str:
    tcp = obs["tcp"][key]
    if key not in obs["api"]:
        return "OK" if tcp["ok"] else "TCP FAIL"
    api = obs["api"][key]
    if not tcp["ok"]:
        return "TCP FAIL"
    if not api["ok"]:
        return "API FAIL"
    if key == "ZPOOL" and api.get("warning"):
        return "WARNING"
    return "OK"


def summary(obs: dict[str, Any], state: dict[str, Any], notes: list[str]) -> str:
    lines = [
        "# External PEPEPOW Pools",
        "",
        f"Checked: {taipei_text(obs.get('checked_at'))}",
        "",
        "| Pool | Stratum | API | Workers | Height | Status |",
        "|---|---|---|---:|---:|---|",
    ]
    for key in ("FOZTOR", "ZPOOL", "PEPEPOW_PPLNS", "PEPEPOW_SOLO", "BOWSERLAB"):
        cfg = POOLS[key]
        tcp = obs["tcp"][key]
        api = obs["api"].get(key)
        api_text = "n/a" if api is None else ("OK" if api["ok"] else "FAIL")
        workers = "-" if api is None or api.get("workers") is None else str(api.get("workers"))
        height = "-" if api is None or api.get("height") is None else str(api.get("height"))
        lines.append(
            f"| {cfg['label']} | {'OK' if tcp['ok'] else 'FAIL'} `{cfg['host']}:{cfg['port']}` | {api_text} | {workers} | {height} | {status_text(obs, key)} |"
        )
    zwarning = str(obs["api"]["ZPOOL"].get("warning", "") or "").strip()
    if zwarning:
        lines += ["", "### zpool PEPEW API warning", "", f"> {zwarning}"]
    active = [name for name, inc in state.get("incidents", {}).items() if inc.get("status") == "ACTIVE"]
    lines += ["", f"Open external-pool incidents: `{len(active)}`" + (f" — {', '.join(active)}" if active else "")]
    if notes:
        lines += ["", "### GitHub Issue notification", *[f"- {note}" for note in notes]]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=".monitor-state/external-pools.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state = load_json(Path(args.state), default_state())
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        state = default_state()

    obs = observe()
    events = process(state, signals(obs))
    notes: list[str] = []
    for event in events:
        delivered, issue_number, note = notify(event, dry_run=args.dry_run)
        notes.append(note)
        if delivered:
            mark(state, event, issue_number)

    save_json(Path(args.state), state)
    text = summary(obs, state, notes)
    print(text)
    if path := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
