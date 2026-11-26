from datetime import datetime
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from keepalive import CHINA_TIME, Keepalive
from minekuai import APIError


class KeepaliveTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.api = Mock(instance="0123abcd")
        self.log = Mock()
        self.job = Keepalive(self.api, Path(folder.name) / "keepalive.json", 6, self.log)

    def test_uses_confirmed_pause_to_schedule_six_days_later(self):
        paused = int(time.time()) - 1000
        self.api.recent_billing_ends.return_value = [paused - 3600, paused]
        self.assertFalse(self.job.step(None))
        self.assertEqual(self.job.read()["next_after"], paused + self.job.age)
        self.assertFalse(self.job.step(None))
        self.api.recent_billing_ends.assert_called_once()
        self.api.resume_billing.assert_not_called()

    def test_short_resume_and_pause_without_starting_game(self):
        self.api.recent_billing_ends.return_value = [int(time.time()) - self.job.age - 1]
        cycle = datetime.fromtimestamp(time.time(), CHINA_TIME).isoformat(timespec="seconds")
        self.api.billing_cycle.side_effect = [None, cycle, cycle, None]
        self.api.power_state.return_value = "offline"
        self.assertTrue(self.job.step(None))
        self.api.resume_billing.assert_called_once()
        self.api.pause_billing.assert_called_once()
        self.api.request_stop.assert_not_called()
        self.assertFalse(self.job.following_up)

    def test_unclear_responses_are_resolved_by_reading_billing(self):
        self.api.recent_billing_ends.return_value = [int(time.time()) - self.job.age - 1]
        cycle = datetime.fromtimestamp(time.time(), CHINA_TIME).isoformat(timespec="seconds")
        self.api.billing_cycle.side_effect = [None, cycle, cycle, None]
        self.api.power_state.return_value = "offline"
        self.api.resume_billing.side_effect = APIError("response timed out")
        self.api.pause_billing.side_effect = APIError("response timed out")
        self.assertTrue(self.job.step(None))
        self.api.resume_billing.assert_called_once()
        self.api.pause_billing.assert_called_once()
        self.assertNotIn("requested_at", self.job.read())

    def test_unknown_history_never_resumes_billing(self):
        self.api.recent_billing_ends.return_value = [None, "unknown"]
        with self.assertRaises(APIError):
            self.job.step(None)
        self.api.resume_billing.assert_not_called()

    def test_pending_attempt_yields_if_game_has_started(self):
        requested_at = time.time()
        self.job.save({"next_after": 0, "requested_at": requested_at, "pause_sent": False})
        self.api.power_state.return_value = "running"
        self.assertTrue(self.job.step(datetime.fromtimestamp(requested_at, CHINA_TIME).isoformat(timespec="seconds")))
        self.api.pause_billing.assert_not_called()
        self.assertNotIn("requested_at", self.job.read())

    def test_paused_confirmation_recovers_without_another_write(self):
        self.job.save({"next_after": 0, "requested_at": time.time(), "pause_sent": True})
        self.assertTrue(self.job.step(None))
        self.api.resume_billing.assert_not_called()
        self.api.pause_billing.assert_not_called()
        self.assertGreater(self.job.read()["next_after"], time.time() + 5 * 86400)
