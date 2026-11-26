import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from minekuai import APIError, RateLimitError
from monitor import Monitor


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = patch("monitor.time.monotonic", return_value=0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.api = Mock(instance="0123abcd")
        self.api.billing_cycle.return_value = "test-cycle"
        self.api.power_state.return_value = "running"
        self.api.request_stop.side_effect = lambda before_send: before_send()
        self.probe = Mock(return_value=0)
        self.monitor = Monitor(self.api, self.probe, Path(self.temp.name) / "state.json", 600, 30)

    def tick(self, at):
        self.now.return_value = at
        self.monitor.tick()

    def samples(self, begin, end):
        for instant in range(begin, end + 1, 30):
            self.tick(instant)

    def test_ten_minutes_then_fresh_confirmation(self):
        self.samples(0, 570)
        self.api.request_stop.assert_not_called()
        self.tick(600)
        self.api.request_stop.assert_called_once()
        self.assertEqual(self.monitor.read_pending()["stage"], "stop_requested")

    def test_unknown_or_join_resets_elapsed_time(self):
        self.samples(0, 300)
        self.probe.return_value = None
        self.tick(330)
        self.probe.return_value = 0
        self.samples(360, 780)
        self.probe.return_value = 1
        self.tick(810)
        self.probe.return_value = 0
        self.samples(840, 1410)
        self.api.request_stop.assert_not_called()
        self.tick(1440)
        self.api.request_stop.assert_called_once()

    def test_poll_gap_does_not_count_as_confirmed_idle(self):
        self.samples(0, 300)
        self.tick(900)
        self.api.request_stop.assert_not_called()

    def test_rejoin_in_final_probe_blocks_write(self):
        self.samples(0, 570)
        self.probe.side_effect = [0, 1]
        self.tick(600)
        self.assertIsNone(self.monitor.read_pending())
        self.assertIsNone(self.monitor.idle_since)

    def test_offline_can_pause_and_verifies_once(self):
        self.monitor.record("stop_requested", "test-cycle")
        self.api.power_state.return_value = "offline"
        self.probe.return_value = None
        self.api.pause_billing.side_effect = lambda: setattr(self.api.billing_cycle, "return_value", None)
        self.tick(0)
        self.api.pause_billing.assert_not_called()
        self.tick(30)
        self.tick(60)
        self.api.pause_billing.assert_called_once()
        self.assertIsNone(self.monitor.read_pending())

    def test_uncertain_billing_write_is_not_repeated(self):
        self.monitor.record("pause_requested", "test-cycle")
        self.tick(0)
        self.tick(30)
        self.api.pause_billing.assert_not_called()

    def test_billing_cycle_change_cancels_old_shutdown(self):
        self.monitor.record("stop_requested", "old-cycle")
        self.tick(0)
        self.api.pause_billing.assert_not_called()
        self.assertIsNone(self.monitor.read_pending())


    def test_dry_run_does_not_write_state_or_send_commands(self):
        self.monitor.dry_run = True
        self.samples(0, 600)
        self.api.request_stop.assert_not_called()
        self.api.pause_billing.assert_not_called()
        self.assertFalse(self.monitor.state_file.exists())

    def test_malformed_pending_operation_is_rejected(self):
        self.monitor.state_file.write_text(
            '{"instance":"0123abcd","stage":[],"cycle":"test-cycle","written_at":0}',
            encoding="utf-8",
        )
        with self.assertRaises(APIError):
            self.tick(0)
        self.api.request_stop.assert_not_called()
        self.api.pause_billing.assert_not_called()


    def test_hourly_check_triggers_on_second_empty_sample(self):
        self.monitor.idle_samples = 2
        self.monitor.poll_seconds = 3600
        self.now.return_value = 0
        self.assertEqual(self.monitor.run_cycle(), 3600)
        self.api.request_stop.assert_not_called()
        self.now.return_value = 3600
        self.assertEqual(self.monitor.run_cycle(), 30)
        self.api.request_stop.assert_called_once()

    def test_hourly_check_resets_on_unknown_player_count_or_new_cycle(self):
        self.monitor.idle_samples = 2
        self.monitor.poll_seconds = 3600
        self.tick(0)
        self.probe.return_value = None
        self.tick(3600)
        self.probe.return_value = 0
        self.tick(7200)
        self.probe.return_value = 1
        self.tick(10800)
        self.probe.return_value = 0
        self.tick(14400)
        self.api.billing_cycle.return_value = "new-cycle"
        self.tick(18000)
        self.api.request_stop.assert_not_called()
        self.tick(21600)
        self.api.request_stop.assert_called_once()

    def test_rate_limit_can_extend_hourly_interval(self):
        self.monitor.idle_samples = 2
        self.monitor.poll_seconds = 3600
        self.tick(0)
        self.api.billing_cycle.side_effect = RateLimitError("7200")
        self.assertEqual(self.monitor.run_cycle(), 7200)
        self.assertEqual(self.monitor.empty_samples, 0)

    def test_hourly_check_resumes_after_billing_pause_is_verified(self):
        self.monitor.idle_samples = 2
        self.monitor.poll_seconds = 3600
        self.monitor.record("stop_requested", "test-cycle")
        self.api.power_state.return_value = "offline"
        self.probe.return_value = None
        self.now.return_value = 0
        self.assertEqual(self.monitor.run_cycle(), 30)
        self.api.pause_billing.side_effect = lambda: setattr(self.api.billing_cycle, "return_value", None)
        self.now.return_value = 30
        self.assertEqual(self.monitor.run_cycle(), 3600)
        self.api.pause_billing.assert_called_once()


if __name__ == "__main__":
    unittest.main()
