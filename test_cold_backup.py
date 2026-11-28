import os
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch
from zipfile import ZipFile

from backup_files import FileSource
from backup_state import store_json
from cold_zip import prune_previous, publish, stored_matches, verify
from minekuai import APIError, instance_operation_lock

if os.name == "posix":
    from cold_backup import ColdBackup


@unittest.skipUnless(os.name == "posix", "backup service runs on Linux")
class ColdBackupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.first = self.directory / "archive-2026-11-07T094213+0800.zip"
        self.second = "archive-2026-11-14T020000+0800.zip"
        self.incoming = self.directory / ".incoming"
        with ZipFile(self.first, "w") as archive:
            archive.writestr("world/level.dat", b"old")

    def test_incomplete_zip_preserves_previous_copy(self):
        self.incoming.write_bytes(b"partial")
        with self.assertRaises(APIError):
            verify(self.incoming, len(b"partial"), ["world"])
        self.assertTrue(self.first.is_file())
        self.assertFalse((self.directory / self.second).exists())

    def test_root_list_mismatch_preserves_previous_copy(self):
        with ZipFile(self.incoming, "w") as archive:
            archive.writestr("world/level.dat", b"new")
        with self.assertRaises(APIError):
            verify(self.incoming, self.incoming.stat().st_size, ["world", "server.properties"])
        self.assertTrue(self.first.is_file())

    def test_verified_replacement_keeps_one_managed_zip(self):
        unrelated = self.directory / "manual.zip"
        unrelated.write_bytes(b"manual")
        with ZipFile(self.incoming, "w") as archive:
            archive.writestr("world/level.dat", b"new")
            archive.writestr("server.properties", b"level-name=world")
        size = self.incoming.stat().st_size
        checksum = verify(self.incoming, size, ["world", "server.properties"])
        final = publish(self.directory, self.incoming, self.second)
        self.assertTrue(self.first.exists())
        prune_previous(self.directory, final)
        self.assertEqual(sorted(p.name for p in self.directory.glob("archive-*.zip")), [self.second])
        self.assertTrue(stored_matches(final, {"size": size, "sha256": checksum}))
        self.assertTrue(unrelated.exists())

    def test_pause_response_is_rechecked_until_visible(self):
        worker = ColdBackup(self.directory / "auth", self.directory / "cold", self.directory)
        journal = self.directory / "billing.json"
        cycle = "2026-11-27T02:00:00+08:00"
        store_json(journal, {"requested_at": 1795716000, "cycle": cycle})
        api = Mock(instance="aa001122")
        api.power_state.return_value = "offline"
        api.billing_cycle.side_effect = [cycle, cycle, cycle, None]
        with patch("cold_backup.time.sleep"):
            worker.settle(api, journal)
        api.pause_billing.assert_called_once_with(timeout=5)
        self.assertFalse(journal.exists())

    def test_running_server_keeps_user_billing_active(self):
        worker = ColdBackup(self.directory / "auth", self.directory / "cold", self.directory)
        journal = self.directory / "billing.json"
        cycle = "2026-11-27T02:00:00+08:00"
        store_json(journal, {"requested_at": 1795716000, "cycle": cycle})
        api = Mock(instance="aa001122")
        api.billing_cycle.return_value = cycle
        api.power_state.return_value = "running"
        worker.settle(api, journal)
        api.pause_billing.assert_not_called()
        self.assertFalse(journal.exists())

    def test_cold_backup_defers_when_shutdown_worker_holds_instance_lock(self):
        worker = ColdBackup(self.directory / "auth", self.directory / "cold", self.directory)
        worker.api_for = Mock()
        with instance_operation_lock(self.directory, "aa001122") as locked:
            self.assertTrue(locked)
            worker.run_instance("aa001122", force=True)
        worker.api_for.assert_not_called()

    def test_nightly_check_only_contacts_panel_for_due_backups(self):
        worker = ColdBackup(self.directory / "auth", self.directory / "cold", self.directory)
        instance = "aa001122"
        worker.inventory.path.write_text(json.dumps({instance: ["example.net", 25565]}), encoding="utf-8")
        area = worker.area(instance)
        archive = area / self.first.name
        archive.write_bytes(self.first.read_bytes())
        store_json(area / "latest.json", {"file": archive.name, "date": "2026-11-07",
                                        "size": archive.stat().st_size})
        worker.inventory.refresh = Mock(return_value=({instance: {}}, {instance: None}))
        worker.run_instance = Mock()
        with patch("cold_backup.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 13, 2)
            worker.run()
            worker.inventory.refresh.assert_not_called()
            worker.run_instance.assert_not_called()
            clock.now.return_value = datetime(2026, 11, 14, 2)
            worker.run()
        worker.inventory.refresh.assert_called_once()
        worker.run_instance.assert_called_once_with(instance)

    def test_missing_remote_task_is_parked_before_another_billing_window(self):
        worker = ColdBackup(self.directory / "auth", self.directory / "cold", self.directory)
        instance = "aa001122"
        area = worker.area(instance)
        task = {"roots": ["world"], "id": "previous-task"}
        store_json(area / "task.json", task)
        worker.list_tasks = Mock(return_value=[])
        with self.assertRaises(APIError):
            worker.finish_archive(Mock(), area, task, "test-cycle")
        self.assertEqual(json.loads((area / "task.json").read_text())["needs_review"], "task_not_found")
        self.assertFalse(worker.backup_due(area))
        worker.api_for = Mock()
        worker.run_instance(instance)
        worker.api_for.assert_not_called()
        with self.assertRaises(APIError):
            worker.run_instance(instance, force=True)
        worker.api_for.return_value.resume_billing.assert_not_called()

    def test_nightly_failure_is_visible_and_does_not_skip_other_instances(self):
        worker = ColdBackup(self.directory / "auth", self.directory / "cold", self.directory)
        failed, other = "aa001122", "aa001123"
        worker.area(failed)
        worker.inventory.refresh = Mock(return_value=({failed: {}, other: {}}, {}))
        worker.run_instance = Mock(side_effect=[APIError("download interrupted"), None])
        with patch("cold_backup.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 28, 2)
            with self.assertRaises(APIError):
                worker.run()
        self.assertEqual(worker.run_instance.call_count, 2)
        self.assertTrue((worker.area(failed) / "failure.json").is_file())

    def test_download_resumes_only_with_matching_range(self):
        source = FileSource.__new__(FileSource)
        source.api = Mock()
        source.api.request.return_value = {"url": "https://node.minekuai.cn/download"}
        source.path = "/panel/files"
        source.node_host = None
        partial = self.incoming.with_suffix(".part")
        partial.write_bytes(b"old")
        with patch("backup_files.http.client.HTTPSConnection") as connect:
            connection = connect.return_value
            response = connection.getresponse.return_value
            response.status = 206
            response.getheader.return_value = "bytes 3-5/6"
            response.read.side_effect = [b"end", b""]
            source.download(Path("/archive.zip"), self.incoming, 6, resume=True)
        connection.request.assert_called_once_with("GET", "/download", headers={"Range": "bytes=3-"})
        self.assertEqual(self.incoming.read_bytes(), b"oldend")
        self.assertFalse(partial.exists())
