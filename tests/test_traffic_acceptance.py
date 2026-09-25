import unittest

from festival_foundation.traffic_acceptance import run


class TrafficAcceptanceTest(unittest.TestCase):
    def test_traffic_relay_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertEqual("closed", result["final_road_status"])
        self.assertTrue(result["entry_chain_valid"])
        self.assertTrue(result["terminal_protected"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["plan_feasible"])
        self.assertEqual("available", result["resource_final_status"])
        self.assertEqual(2, result["lease_count"])
        self.assertGreaterEqual(result["late_entries"], 1)


if __name__ == "__main__":
    unittest.main()
