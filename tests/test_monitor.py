from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "monitor" / "pepepow_monitor.py"
spec = importlib.util.spec_from_file_location("pepepow_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["pepepow_monitor"] = monitor
spec.loader.exec_module(monitor)


class DummyResult:
    def __init__(self, ok=True, data=None, error=None, status=None, text=""):
        self.ok = ok
        self.data = data if data is not None else {}
        self.error = error
        self.status = 200 if ok and status is None else status
        self.text = text
        self.latency_ms = 1


CONFIG = {
    "consecutive_failures": 2,
    "chain_stall_seconds": 1800,
    "payment_stale_hours": 3,
    "hashrate_drop_percent": 80,
    "sources": {
        "explorer_status": "explorer-api",
        "light_status": "light-api",
        "explorer_net": "net",
        "explorer_org": "org",
        "pool_health": "pool-health",
        "pool_summary": "pool-summary",
        "pool_payments": "payments"
    }
}


def obs(explorer_height=100, light_height=100, *, explorer_ok=True, light_ok=True,
        chain_moving="moving", explicit_stall=False, last_block_age=60, hashrate=1000,
        net_site=True, org_site=True, org_cf=False, pool_ok=True, payments_ok=True,
        pool_block_marker="b1", payment_marker="p1"):
    results = {
        "explorer": DummyResult(explorer_ok),
        "light": DummyResult(light_ok),
        "light_health": DummyResult(light_ok),
        "explorer_net_site": DummyResult(net_site),
        "explorer_org_site": DummyResult(org_site),
        "pool_health": DummyResult(pool_ok),
        "pool_summary": DummyResult(pool_ok),
        "pool_network": DummyResult(pool_ok),
        "pool_blocks": DummyResult(pool_ok),
        "pool_payments": DummyResult(payments_ok),
    }
    if org_cf:
        results["explorer_org_site"] = DummyResult(
            False,
            error="HTTP 403",
            status=403,
            text="<!doctype html><title>Just a moment...</title> Cloudflare",
        )
    return {
        "checked_at": "2026-08-16T00:00:00Z",
        "results": results,
        "explorer_height": explorer_height,
        "light_height": light_height,
        "explorer_hashrate": hashrate,
        "last_block_age": last_block_age,
        "explorer_explicit_stall": explicit_stall,
        "explorer_chain_moving": chain_moving,
        "light_ok": light_ok,
        "pool_stale": False,
        "pool_degraded": False,
        "pool_status": "ready",
        "pool_block_marker": pool_block_marker,
        "payment_marker": payment_marker,
    }


def signal_map(signals):
    return {s.name: s for s in signals}


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 16, 5, 0, tzinfo=timezone.utc)

    def test_both_moving_is_normal(self):
        state = monitor.default_state()
        state["last_values"] = {"explorer_height": 90, "light_height": 90, "explorer_hashrate": 1000}
        sm = signal_map(monitor.evaluate(obs(100, 100), state, CONFIG, self.now))
        self.assertFalse(sm["EXPLORER_NODE_STALE"].active)
        self.assertFalse(sm["LIGHT_NODE_STALE"].active)
        self.assertFalse(sm["NETWORK_CHAIN_STALL"].active)

    def test_explorer_stuck_light_moving(self):
        state = monitor.default_state()
        state["last_values"] = {"explorer_height": 100, "light_height": 90, "explorer_hashrate": 1000}
        sm = signal_map(monitor.evaluate(obs(100, 110), state, CONFIG, self.now))
        self.assertTrue(sm["EXPLORER_NODE_STALE"].active)
        self.assertFalse(sm["NETWORK_CHAIN_STALL"].active)

    def test_light_stuck_explorer_moving(self):
        state = monitor.default_state()
        state["last_values"] = {"explorer_height": 90, "light_height": 100, "explorer_hashrate": 1000}
        sm = signal_map(monitor.evaluate(obs(110, 100), state, CONFIG, self.now))
        self.assertTrue(sm["LIGHT_NODE_STALE"].active)

    def test_both_unchanged_without_stall_evidence_is_not_chain_stall(self):
        state = monitor.default_state()
        state["last_values"] = {"explorer_height": 100, "light_height": 100, "explorer_hashrate": 1000}
        sm = signal_map(monitor.evaluate(obs(100, 100, last_block_age=300), state, CONFIG, self.now))
        self.assertFalse(sm["NETWORK_CHAIN_STALL"].active)

    def test_confirmed_chain_stall(self):
        state = monitor.default_state()
        state["last_values"] = {"explorer_height": 100, "light_height": 100, "explorer_hashrate": 1000}
        sm = signal_map(monitor.evaluate(obs(100, 100, explicit_stall=True, chain_moving="stalled", last_block_age=2000), state, CONFIG, self.now))
        self.assertTrue(sm["NETWORK_CHAIN_STALL"].active)
        self.assertEqual(sm["NETWORK_CHAIN_STALL"].severity, "CRITICAL")
        self.assertTrue(sm["NETWORK_CHAIN_STALL"].immediate)

    def test_both_explorers_down_combined(self):
        state = monitor.default_state()
        state["last_values"] = {"explorer_height": 90, "light_height": 90, "explorer_hashrate": 1000}
        sm = signal_map(monitor.evaluate(obs(100, 100, net_site=False, org_site=False), state, CONFIG, self.now))
        self.assertTrue(sm["PUBLIC_EXPLORERS_DOWN"].active)
        self.assertFalse(sm["EXPLORER_NET_SITE_DOWN"].active)
        self.assertFalse(sm["EXPLORER_ORG_SITE_DOWN"].active)

    def test_cloudflare_challenge_is_not_org_outage(self):
        state = monitor.default_state()
        state["last_values"] = {"explorer_height": 90, "light_height": 90, "explorer_hashrate": 1000}
        sm = signal_map(monitor.evaluate(obs(100, 100, org_cf=True), state, CONFIG, self.now))
        self.assertFalse(sm["EXPLORER_ORG_SITE_DOWN"].active)
        self.assertFalse(sm["PUBLIC_EXPLORERS_DOWN"].active)

    def test_payment_stall_requires_block_progress_and_elapsed_time(self):
        state = monitor.default_state()
        state["last_values"] = {"explorer_height": 90, "light_height": 90, "explorer_hashrate": 1000}
        state["tracking"] = {
            "payment_last_change_at": "2026-08-16T01:00:00Z",
            "pool_block_last_change_at": "2026-08-16T04:30:00Z"
        }
        sm = signal_map(monitor.evaluate(obs(100, 100), state, CONFIG, self.now))
        self.assertTrue(sm["POSSIBLE_PAYMENT_STALL"].active)

    def test_hashrate_drop(self):
        state = monitor.default_state()
        state["last_values"] = {"explorer_height": 90, "light_height": 90, "explorer_hashrate": 1000}
        sm = signal_map(monitor.evaluate(obs(100, 100, hashrate=100), state, CONFIG, self.now))
        self.assertTrue(sm["NETWORK_HASHRATE_COLLAPSE"].active)


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 16, 5, 0, tzinfo=timezone.utc)

    def test_warning_requires_two_runs_and_deduplicates(self):
        state = monitor.default_state()
        sig = [monitor.Signal("TEST", True, evidence={"x": 1})]
        first = monitor.process_incidents(state, sig, CONFIG, self.now)
        self.assertEqual(first, [])
        second = monitor.process_incidents(state, sig, CONFIG, self.now)
        self.assertEqual(len(second), 1)
        monitor.mark_event_notified(state, second[0], 17)
        self.assertEqual(state["incidents"]["TEST"]["issue_number"], 17)
        third = monitor.process_incidents(state, sig, CONFIG, self.now)
        self.assertEqual(third, [])

    def test_recovery_once(self):
        state = monitor.default_state()
        sig = [monitor.Signal("TEST", True)]
        monitor.process_incidents(state, sig, CONFIG, self.now)
        alert = monitor.process_incidents(state, sig, CONFIG, self.now)[0]
        monitor.mark_event_notified(state, alert, 17)
        recovered = monitor.process_incidents(state, [monitor.Signal("TEST", False)], CONFIG, self.now)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["type"], "RECOVERY")
        self.assertEqual(recovered[0]["issue_number"], 17)
        monitor.mark_event_notified(state, recovered[0], 17)
        again = monitor.process_incidents(state, [monitor.Signal("TEST", False)], CONFIG, self.now)
        self.assertEqual(again, [])

    def test_critical_immediate(self):
        state = monitor.default_state()
        events = monitor.process_incidents(state, [monitor.Signal("STALL", True, severity="CRITICAL", immediate=True)], CONFIG, self.now)
        self.assertEqual(len(events), 1)


class NotificationTests(unittest.TestCase):
    def alert_event(self, issue_number=None):
        return {
            "type": "ALERT",
            "name": "EXPLORER_NODE_STALE",
            "status": "ACTIVE",
            "severity": "WARNING",
            "opened_at": "2026-08-16T04:00:00Z",
            "last_seen_at": "2026-08-16T05:00:00Z",
            "evidence": {"explorer_height": 100, "light_height": 110},
            "url": "https://explorer.pepepow.net/monitor/api/status",
            "issue_number": issue_number,
        }

    def test_alert_issue_is_assigned_to_repo_owner(self):
        calls = []

        def fake_api(method, path, payload=None):
            calls.append((method, path, payload))
            return True, {"number": 23}, "ok"

        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "edisontw/pepepow-monitor"}, clear=False):
            with mock.patch.object(monitor, "github_api", side_effect=fake_api):
                ok, number, note = monitor.notify_github_issue(self.alert_event(), obs(), dry_run=False)

        self.assertTrue(ok)
        self.assertEqual(number, 23)
        self.assertEqual(note, "Issue #23 created")
        self.assertEqual(calls[0][0:2], ("POST", "issues"))
        self.assertEqual(calls[0][2]["assignees"], ["edisontw"])
        self.assertIn("[PEPEPOW ALERT] WARNING", calls[0][2]["title"])

    def test_existing_issue_gets_update_instead_of_duplicate(self):
        with mock.patch.object(monitor, "github_api", return_value=(True, {}, "ok")) as api:
            ok, number, _ = monitor.notify_github_issue(self.alert_event(23), obs(), dry_run=False)
        self.assertTrue(ok)
        self.assertEqual(number, 23)
        api.assert_called_once()
        self.assertEqual(api.call_args.args[0:2], ("POST", "issues/23/comments"))

    def test_recovery_comments_then_closes_issue(self):
        event = self.alert_event(23)
        event["type"] = "RECOVERY"
        event["recovered_at"] = "2026-08-16T05:30:00Z"
        calls = []

        def fake_api(method, path, payload=None):
            calls.append((method, path, payload))
            return True, {}, "ok"

        with mock.patch.object(monitor, "github_api", side_effect=fake_api):
            ok, number, note = monitor.notify_github_issue(event, obs(), dry_run=False)

        self.assertTrue(ok)
        self.assertEqual(number, 23)
        self.assertEqual(note, "Issue #23 recovered and closed")
        self.assertEqual(calls[0][0:2], ("POST", "issues/23/comments"))
        self.assertEqual(calls[1][0:2], ("PATCH", "issues/23"))
        self.assertEqual(calls[1][2]["state"], "closed")

    def test_manual_dry_run_never_calls_github(self):
        with mock.patch.object(monitor, "github_api") as api:
            ok, number, note = monitor.notify_github_issue(self.alert_event(), obs(), dry_run=True)
        self.assertFalse(ok)
        self.assertIsNone(number)
        self.assertIn("dry-run", note)
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
