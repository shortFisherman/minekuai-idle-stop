import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor import Config
from status import collect_status, last_probe


class StatusTests(unittest.TestCase):
    def test_summarizes_local_files_without_contacting_minekuai(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = "0123abcd"
            auth = root / "auth.json"
            auth.write_text('{"token":"dummy-session","clientid":"demo"}', encoding="utf-8")
            (root / "instances.json").write_text(json.dumps({instance: ["node.example.net", 25565]}),
                                                  encoding="utf-8")
            backup = root / "instances" / instance
            backup.mkdir(parents=True)
            archive = backup / "archive-2026-11-27T094301+0800.zip"
            archive.write_bytes(b"zip")
            (backup / "latest.json").write_text(json.dumps({"file": archive.name, "size": 3,
                                                           "date": "2026-11-27"}), encoding="utf-8")
            (backup / "billing.json").touch()
            (backup / "failure.json").write_text('{"at":1795829824}', encoding="utf-8")
            (backup / "task.json").write_text('{"needs_review":"task_not_found"}', encoding="utf-8")
            (root / f"state-{instance}.json").touch()
            (root / f"keepalive-{instance}.json").write_text('{"requested_at":1795829824}', encoding="utf-8")
            config = Config(None, None, None, 600, 3600, 2, 6)
            with patch("status.unit_properties") as units, patch("status.last_probe", return_value=None), \
                    patch("minekuai.http.client.HTTPSConnection") as connection:
                units.side_effect = [{"ActiveState": "active"},
                                     {"LoadState": "loaded", "ActiveState": "active"}, {"Result": "success"}]
                result = collect_status(config, root / "state.json", auth, root)
                connection.assert_not_called()
            self.assertEqual(result["monitor"]["known_instances"], 1)
            self.assertEqual(result["minekuai_session"], "no_rejection_recorded")
            self.assertEqual(result["cold_backup"]["instances_with_verified_archive"], 1)
            self.assertEqual(result["cold_backup"]["pending_billing_cleanups"], 1)
            self.assertTrue(result["keepalive"]["has_instances_to_recheck"])
            self.assertEqual(result["instances"], [{
                "instance_suffix": "abcd", "backup_date": "2026-11-27",
                "backup_failed_at": "2026-11-28T09:37:04+08:00", "backup_blocked": "task_not_found",
                "pending_billing": ["shutdown", "keepalive", "cold_backup"],
            }])
            self.assertNotIn("dummy-session", json.dumps(result))
            self.assertNotIn("node.example.net", json.dumps(result))
            self.assertNotIn(instance, json.dumps(result))

    def test_last_probe_uses_completed_round_from_journal(self):
        record = {"MESSAGE": "INFO 游戏人数检查完成：探测 3 台，有效人数 1 台，名单共 4 台",
                  "__REALTIME_TIMESTAMP": "1795829824000000"}
        with patch("status.system_output", return_value=json.dumps(record)):
            self.assertEqual(last_probe(), {"completed_at": "2026-11-28T09:37:04+08:00", "probed": 3,
                                           "responses": 1, "known": 4})

    def test_reports_unknown_before_first_round(self):
        with patch("status.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertIsNone(last_probe())
