import os
import tempfile
import unittest
from pathlib import Path
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
