#!/usr/bin/env python3
"""Best-effort public website/image probe for https://pepepow.org/.

Designed for GitHub Actions. Cloudflare challenges are reported as monitoring
unavailable rather than as an outage. No private endpoints or credentials are used.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SITE_URL = "https://pepepow.org/"
USER_AGENT = "PEPEPOW-GitHub-Monitor/3.1 (+https://github.com/edisontw/pepepow-monitor)"
TAIPEI = ZoneInfo("Asia/Taipei")
STATE_PATH = Path(".monitor-state/pepepow-org-site.json")
CONSECUTIVE_FAILURES = 2
MAX_IMAGES = 12
TIMEOUT = 12


@dataclass
class ProbeResult:
    url: str
    ok: bool
    status: int | None = None
    content_type: str = ""
    text: str = ""
    error: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def taipei_now() -> str:
    return datetime.now(timezone.utc).astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S %Z")


def is_cloudflare(status: int | None, text: str) -> bool:
    if status not in {403, 429, 503}:
        return False
    lowered = (text or "").lower()
    return any(marker in lowered for marker in ("just a moment", "cf-chl-", "cloudflare"))


def fetch_page(url: str) -> ProbeResult:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read(3_000_000)
            text = raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            status = getattr(response, "status", 200)
            content_type = response.headers.get("Content-Type", "")
            obvious_error = any(x in text[:20000].lower() for x in (
                "502 bad gateway", "503 service unavailable", "internal server error"
            ))
            return ProbeResult(url, 200 <= status < 400 and not obvious_error,
                               status=status, content_type=content_type, text=text,
                               error="obvious_error_page" if obvious_error else None)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(20000).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return ProbeResult(url, False, status=exc.code,
                           content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
                           text=body, error=f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ProbeResult(url, False, error=f"{exc.__class__.__name__}: {exc}")


class ImageCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def add(self, value: str | None) -> None:
        if not value:
            return
        value = value.strip()
        if value and value not in self.urls and not value.startswith(("data:", "blob:")):
            self.urls.append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): v for k, v in attrs}
        tag = tag.lower()
        if tag == "img":
            for key in ("src", "data-src", "data-lazy-src", "data-original"):
                self.add(a.get(key))
            srcset = a.get("srcset") or a.get("data-srcset")
            if srcset:
                for candidate in srcset.split(","):
                    self.add(candidate.strip().split(" ", 1)[0])
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
                self.add(a.get("content"))


def image_urls(html: str, base_url: str) -> list[str]:
    parser = ImageCollector()
    try:
        parser.feed(html)
    except Exception:
        return []
    resolved: list[str] = []
    for value in parser.urls:
        url = urllib.parse.urljoin(base_url, value)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc and url not in resolved:
            resolved.append(url)
    return resolved


def probe_image(url: str) -> ProbeResult:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": SITE_URL,
            "Range": "bytes=0-2047",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            status = getattr(response, "status", 200)
            content_type = response.headers.get("Content-Type", "").lower()
            raw = response.read(2048)
            text = raw.decode("utf-8", errors="replace") if "html" in content_type else ""
            html_instead_of_image = "text/html" in content_type or is_cloudflare(status, text)
            return ProbeResult(url, 200 <= status < 400 and not html_instead_of_image,
                               status=status, content_type=content_type, text=text,
                               error="html_instead_of_image" if html_instead_of_image else None)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(3000).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return ProbeResult(url, False, status=exc.code,
                           content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
                           text=body, error=f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ProbeResult(url, False, error=f"{exc.__class__.__name__}: {exc}")


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def github_api(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[bool, Any]:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        return False, None
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/{path.lstrip('/')}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(1_000_000)
            return True, json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return False, None


def incident(state: dict[str, Any], key: str, active: bool, evidence: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    item = state.setdefault(key, {"failures": 0, "issue_number": None, "active": False})
    if active:
        item["failures"] = int(item.get("failures", 0)) + 1
        item["evidence"] = evidence
        if item["failures"] >= CONSECUTIVE_FAILURES and not item.get("active"):
            title = f"[PEPEPOW ALERT] WARNING - {key}"
            body = "\n".join([
                "PEPEPOW public website monitor detected a persistent issue.",
                "",
                f"- Time: {taipei_now()}",
                f"- Incident: `{key}`",
                f"- URL: {SITE_URL}",
                "",
                "```json",
                json.dumps(evidence, indent=2, ensure_ascii=False, sort_keys=True),
                "```",
                "",
                "Only public HTTP observations are included; no root cause is inferred.",
            ])
            repo = os.getenv("GITHUB_REPOSITORY", "")
            owner = repo.split("/", 1)[0] if "/" in repo else ""
            payload: dict[str, Any] = {"title": title, "body": body}
            if owner:
                payload["assignees"] = [owner]
            ok, result = github_api("POST", "issues", payload)
            if ok and isinstance(result, dict) and result.get("number"):
                item["issue_number"] = int(result["number"])
                item["active"] = True
                notes.append(f"Issue #{item['issue_number']} created for {key}")
            else:
                notes.append(f"Could not create Issue for {key}; will retry")
    else:
        item["failures"] = 0
        if item.get("active"):
            number = item.get("issue_number")
            if number:
                comment = f"RECOVERED at {taipei_now()}. Public probe is healthy again."
                ok1, _ = github_api("POST", f"issues/{number}/comments", {"body": comment})
                ok2, _ = github_api("PATCH", f"issues/{number}", {"state": "closed", "state_reason": "completed"})
                if ok1 and ok2:
                    notes.append(f"Issue #{number} recovered and closed")
                    item["active"] = False
                    item["issue_number"] = None
            else:
                item["active"] = False
    return notes


def main() -> int:
    state = load_state()
    page = fetch_page(SITE_URL)
    page_cf = is_cloudflare(page.status, page.text)
    notes: list[str] = []

    # A Cloudflare challenge means GitHub-hosted monitoring is unavailable, not that
    # the public website is definitely down. Per design, do not open an incident.
    site_down = (not page.ok) and (not page_cf)
    notes += incident(state, "PEPEPOW_ORG_SITE_DOWN", site_down, {
        "status": page.status,
        "error": page.error,
    })

    checked_images: list[ProbeResult] = []
    broken_same_host: list[ProbeResult] = []
    external_failures = 0
    if page.ok:
        base_host = urllib.parse.urlparse(SITE_URL).hostname
        for url in image_urls(page.text, SITE_URL)[:MAX_IMAGES]:
            result = probe_image(url)
            checked_images.append(result)
            same_host = urllib.parse.urlparse(url).hostname == base_host
            if not result.ok:
                if same_host:
                    # A same-site image that returns 403/404/5xx or HTML instead of
                    # image bytes is a meaningful public rendering problem.
                    broken_same_host.append(result)
                else:
                    external_failures += 1

    image_evidence = {
        "checked": len(checked_images),
        "broken_same_host": len(broken_same_host),
        "external_failures_not_alerting": external_failures,
        "samples": [
            {"url": r.url, "status": r.status, "error": r.error, "content_type": r.content_type}
            for r in broken_same_host[:5]
        ],
    }
    # Skip image alerting if the page itself cannot be fetched; that is already
    # represented by the site status or CF-BLOCKED.
    notes += incident(state, "PEPEPOW_ORG_IMAGES_BROKEN", page.ok and bool(broken_same_host), image_evidence)

    state["last_checked_at"] = now_iso()
    state["last_page_status"] = page.status
    state["last_page_cf_blocked"] = page_cf
    state["last_image_summary"] = image_evidence
    save_state(state)

    page_status = "OK" if page.ok else ("CF-BLOCKED (not alerting)" if page_cf else "FAIL")
    lines = [
        "## pepepow.org website probe",
        "",
        "| Component | Status |",
        "|---|---|",
        f"| pepepow.org | {page_status} |",
    ]
    if page.ok:
        image_status = "OK" if not broken_same_host else f"FAIL ({len(broken_same_host)} same-site image(s))"
        lines.append(f"| Homepage/OG images | {image_status} |")
        lines += [
            "",
            f"- Images sampled: `{len(checked_images)}`",
            f"- Same-site image failures: `{len(broken_same_host)}`",
            f"- External image failures (informational only): `{external_failures}`",
        ]
    else:
        lines.append("| Homepage/OG images | NOT TESTED |")
    if notes:
        lines += ["", *[f"- {note}" for note in notes]]
    summary = "\n".join(lines) + "\n"
    print(summary)
    if path := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n" + summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
