import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from minekuai import APIError, Minekuai
from monitor import AccountMonitor, Config, load_config

IDS = ("0123abcd", "0123abce", "0123abcf")


class AccountTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        config = Config(None, None, None, 600, 3600, 2, 6, (IDS[0],))
        self.account = AccountMonitor(config, self.path / "auth.json", self.path / "state.json")
        self.account.account.billing_instances = Mock()
        self.account.account.billing_instances.return_value = {
            instance: {"timingStatus": 1, "lastStartTime": "cycle-" + instance} for instance in IDS
        }
        self.clock = patch("monitor.time.monotonic", return_value=0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        endpoints = patch.object(Minekuai, "instance_endpoint", return_value=("node.example.net", 25565))
        self.endpoints = endpoints.start()
        self.addCleanup(endpoints.stop)

    def test_shared_poll_and_automatic_discovery(self):
        accounts = self.account.account.billing_instances
        with patch("monitor.online_count", return_value=1):
            self.assertEqual(self.account.run_cycle(), 3600)
            accounts.assert_called_once()
            self.assertEqual(set(self.account.targets), set(IDS))
            self.assertEqual(self.endpoints.call_count, 3)
            self.account.account.billing_instances.return_value["0123ab01"] = {
                "timingStatus": 1, "lastStartTime": "new-cycle"
            }
            self.now.return_value = 3600
            self.assertEqual(self.account.run_cycle(), 3600)
            self.assertEqual(accounts.call_count, 2)
            self.assertEqual(self.endpoints.call_count, 4)
            self.assertEqual(len(self.account.targets), 4)

    def test_keeps_counters_and_keepalive_files_per_instance(self):
        for instance in IDS:
            target = self.account.target(instance, 0)
            self.assertEqual(target.state_file.name, f"state-{instance}.json")
            if instance == IDS[0]:
                self.assertEqual(target.keepalive.path.name, f"keepalive-{instance}.json")
            else:
                self.assertIsNone(target.keepalive)
        self.account.targets[IDS[0]][0].empty_samples = 1
        self.assertEqual(self.account.targets[IDS[1]][0].empty_samples, 0)

    def test_followup_observes_only_the_target(self):
        for instance in IDS:
            target = self.account.target(instance, 0)
            target.run_cycle = Mock(return_value=3600)
        self.account.next_regular = 3600
        self.account.followups[IDS[1]] = 30
        self.now.return_value = 30
        self.account.run_cycle()
        self.account.targets[IDS[0]][0].run_cycle.assert_not_called()
        self.account.targets[IDS[1]][0].run_cycle.assert_called_once_with("cycle-" + IDS[1], raise_on_limit=True)
        self.account.targets[IDS[2]][0].run_cycle.assert_not_called()
        self.account.account.billing_instances.assert_called_once()

    def test_account_mode_uses_no_fixed_addresses(self):
        path = self.path / "config.json"
        path.write_text('{"scope":"account","idle_samples":2,"poll_seconds":3600}', encoding="utf-8")
        result = load_config(path)
        self.assertIsNone(result.instance)
        self.assertIsNone(result.host)
        self.assertIsNone(result.port)

    def test_owner_and_primary_address_are_verified(self):
        api = Minekuai(IDS[0], self.path / "auth.json")
        data = {"meta": {"is_server_owner": True}, "attributes": {
            "identifier": IDS[0], "is_time_based": 1,
            "relationships": {"allocations": {"data": [{"attributes": {
                "ip": "0.0.0.0", "ip_alias": "node.example.net", "port": 30001, "is_default": True,
            }}]}},
        }}
        self.assertEqual(api.endpoint_from_panel(data), ("node.example.net", 30001))
        data["meta"]["is_server_owner"] = False
        with self.assertRaises(APIError):
            api.endpoint_from_panel(data)
