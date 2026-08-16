from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "monitor" / "external_pool_probe.py"
MONITOR_DIR = str(MODULE_PATH.parent)
if MONITOR_DIR not in sys.path:
    sys.path.insert(0, MONITOR_DIR)
spec = importlib.util.spec_from_file_location("external_pool_probe", MODULE_PATH)
probe = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["external_pool_probe"] = probe
spec.loader.exec_module(probe)


class ParserTests(unittest.TestCase):
    def test_foztor_pool_entry(self):
        data = {
            "pools": {
                "hoohashv110-pepew": {
                    "symbol": "PEPEW",
                    "algorithm": "hoohashv110",
                    "workerCount": 4,
                    "hashrate": 12345,
                }
            }
        }
        result = probe.foztor_health(data)
        self.assertTrue(result["ok"])
        self.assertEqual(result["workers"], 4)

    def test_yiimp_zero_workers_is_healthy(self):
        status = {"hoohash-pepew": {"port": 9912, "workers": 0, "hashrate": 0}}
        currencies = {"PEPEW": {"port": "9912", "height": 123, "error": ""}}
        result = probe.yiimp_health(status, currencies, expected_port=9912)
        self.assertTrue(result["ok"])
        self.assertEqual(result["workers"], 0)

    def test_yiimp_port_mismatch_is_unhealthy(self):
        status = {"hoohash-pepew": {"port": 9999, "workers": 1}}
        currencies = {"PEPEW": {"port": 9999}}
        result = probe.yiimp_health(status, currencies, expected_port=9912)
        self.assertFalse(result["ok"])

    def test_zpool_warning_is_preserved(self):
        status = {"hoohash-pepew": {"port": 8335, "workers": 2}}
        currencies = {"PEPEW": {"port": 8335, "height": 456, "error": "currency shortage"}}
        result = probe.yiimp_health(status, currencies, expected_port=8335)
        self.assertTrue(result["ok"])
        self.assertEqual(result["warning"], "currency shortage")


class IncidentTests(unittest.TestCase):
    def test_requires_two_consecutive_failures(self):
        state = probe.default_state()
        current = {"TEST": {"active": True, "label": "Test", "evidence": {"x": 1}}}
        self.assertEqual(probe.process(state, current), [])
        events = probe.process(state, current)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "ALERT")

    def test_recovery_after_active_incident(self):
        state = probe.default_state()
        bad = {"TEST": {"active": True, "label": "Test", "evidence": {}}}
        probe.process(state, bad)
        alert = probe.process(state, bad)[0]
        probe.mark(state, alert, 7)
        events = probe.process(state, {"TEST": {"active": False, "label": "Test", "evidence": {}}})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "RECOVERY")
        self.assertEqual(events[0]["issue_number"], 7)


if __name__ == "__main__":
    unittest.main()
