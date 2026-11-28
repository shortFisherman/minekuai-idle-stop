import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from minekuai import APIError, AuthError, Minekuai
from monitor import AccountMonitor, Config, load_config

IDS = ("0123abcd", "0123abce", "0123abcf")
ENDPOINT = ("node.example.net", 25565)


class AccountTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        config = Config(None, None, None, 600, 3600, 2, 6, (IDS[0],))
        self.account = AccountMonitor(config, self.path / "auth.json", self.path / "state.json")
        self.account.account.billing_instances = Mock()
        self.account.account.billing_instances.return_value = {
            instance: {"timingStatus": 1, "lastStartTime": "1700000000"} for instance in IDS
        }
        self.account.inventory.path.write_text(json.dumps(dict.fromkeys(IDS, ENDPOINT)), encoding="utf-8")
        self.clock = patch("monitor.time.monotonic", return_value=0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        endpoints = patch.object(Minekuai, "instance_endpoint", return_value=ENDPOINT)
        self.endpoints = endpoints.start()
        self.addCleanup(endpoints.stop)

    def test_hourly_probe_and_updated_roster_are_local_only(self):
        self.account.target(IDS[0], ENDPOINT).keepalive.clear(86400)
        accounts = self.account.account.billing_instances
        with patch("monitor.online_count", return_value=1) as probe:
            self.assertEqual(self.account.run_cycle(), 3600)
            self.assertEqual(probe.call_count, 3)
            self.assertEqual(set(self.account.targets), set(IDS))
            roster = dict.fromkeys((*IDS[1:], "0123ab01"), ENDPOINT)
            self.account.inventory.path.write_text(json.dumps(roster), encoding="utf-8")
            self.now.return_value = 60
            self.account.run_cycle()
            self.assertEqual(set(self.account.targets), set(roster))
            self.now.return_value = 3600
            self.assertEqual(self.account.run_cycle(), 3600)
            self.assertEqual(probe.call_count, 6)
        accounts.assert_not_called()
        self.endpoints.assert_not_called()

    def test_due_keepalive_refreshes_roster_once(self):
        accounts = self.account.account.billing_instances
        accounts.return_value["0123ab01"] = {"timingStatus": 0}
        with patch("monitor.online_count", return_value=None):
            self.account.run_cycle()
            self.assertIn("0123ab01", self.account.targets)
            self.now.return_value = 3600
            self.account.run_cycle()
        accounts.assert_called_once_with(strict=True)
        self.assertEqual(self.endpoints.call_count, 4)

    def test_second_zero_bypasses_panel_until_candidate_and_rechecks_cycle(self):
        self.account.target(IDS[0], ENDPOINT).keepalive.clear(86400)
        targets = [self.account.target(instance, ENDPOINT) for instance in IDS]
        for target in targets:
            target.stop_idle = Mock()
        with patch("monitor.online_count", return_value=0), patch("monitor.time.time", return_value=1700000100):
            # The synthetic clock is earlier than the stored keepalive date.
            self.account.run_cycle()
            self.account.account.billing_instances.assert_not_called()
            self.account.account.billing_instances.return_value[IDS[1]]["lastStartTime"] = "1700000300"
            self.now.return_value = 3600
            self.account.run_cycle()
        self.account.account.billing_instances.assert_called_once_with(strict=True)
        targets[0].stop_idle.assert_called_once_with("1700000000")
        targets[1].stop_idle.assert_not_called()
        targets[2].stop_idle.assert_called_once_with("1700000000")

    def test_keeps_counters_and_keepalive_files_per_instance(self):
        for instance in IDS:
            target = self.account.target(instance, ENDPOINT)
            self.assertEqual(target.state_file.name, f"state-{instance}.json")
            if instance == IDS[0]:
                self.assertEqual(target.keepalive.path.name, f"keepalive-{instance}.json")
            else:
                self.assertIsNone(target.keepalive)
        self.account.targets[IDS[0]].empty_samples = 1
        self.assertEqual(self.account.targets[IDS[1]].empty_samples, 0)

    def test_address_change_during_keepalive_discards_queued_stop(self):
        with patch("monitor.time.time", return_value=1700001000) as wall, patch("monitor.online_count", return_value=0):
            self.account.target(IDS[0], ENDPOINT).keepalive.clear(1800)
            self.account.run_cycle()
            self.now.return_value = 3600
            wall.return_value += 3600
            new_endpoint = ("new-node.example.net", 25565)
            with patch.object(Minekuai, "instance_endpoint",
                              lambda api: new_endpoint if api.instance == IDS[1] else ENDPOINT), \
                    patch("monitor.Monitor.stop_idle") as stop:
                self.account.run_cycle()
        self.assertEqual(self.account.targets[IDS[1]].api.expected_endpoint, new_endpoint)
        self.assertIsNone(self.account.targets[IDS[1]].first_empty_at)
        stop.assert_called_once()  # The third instance's address and samples remain valid.

    def test_followup_observes_only_the_target(self):
        target = self.account.target(IDS[1], ENDPOINT)
        target.record("stop_requested", "1700000000")
        self.account.perform = Mock()
        self.account.next_regular = 3600
        self.account.followups[IDS[1]] = 30
        self.now.return_value = 30
        self.account.run_cycle()
        self.account.perform.assert_called_once_with(target, "1700000000", "finish_stop")
        self.account.account.billing_instances.assert_called_once_with(strict=True)

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

    def test_rejected_session_stays_blocked_across_workers_and_restarts(self):
        monitor_auth = self.path / "systemd-session.json"
        backup_auth = self.path / "auth.json"
        rejected = {"token": "old-session", "clientid": "client"}
        for path in (monitor_auth, backup_auth):
            path.write_text(json.dumps(rejected), encoding="utf-8")

        account = Minekuai(None, monitor_auth, state_directory=self.path)
        backup = Minekuai(IDS[0], backup_auth, state_directory=self.path)
        with patch("minekuai.http.client.HTTPSConnection") as connect:
            response = connect.return_value.getresponse.return_value
            response.status = 401
            with self.assertRaisesRegex(AuthError, "重新授权"):
                account.billing_instances()
            with self.assertRaises(AuthError):
                backup.resume_billing()
            restarted = Minekuai(None, monitor_auth, state_directory=self.path)
            with self.assertRaises(AuthError):
                restarted.billing_instances()
            connect.assert_called_once()

            backup_auth.write_text(json.dumps({**rejected, "token": "new-session"}), encoding="utf-8")
            response.status = 200
            response.read.return_value = b'{"code": 200, "data": []}'
            self.assertEqual(backup.billing_instances(), {})
            with self.assertRaises(AuthError):
                restarted.billing_instances()
            self.assertEqual(connect.call_count, 2)

    def test_auth_business_code_blocks_later_requests(self):
        auth = self.path / "auth.json"
        auth.write_text('{"token":"demo-session","clientid":"client"}', encoding="utf-8")
        account = Minekuai(None, auth, state_directory=self.path)
        with patch("minekuai.http.client.HTTPSConnection") as connect:
            response = connect.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"code": "419"}'
            with self.assertRaises(AuthError):
                account.billing_instances()
            with self.assertRaises(AuthError):
                account.ensure_session_usable()
            connect.assert_called_once()
