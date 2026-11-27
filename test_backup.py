import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from backup_state import BackupRetirement, CONFIRM_MISSING_AFTER, RETIRE_AFTER, read_state
from minekuai import Minekuai


@unittest.skipUnless(os.name == "posix", "backup service runs on Linux")
class BackupRetirementTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.backups = BackupRetirement(Minekuai("account", root / "auth.json"), root / "instances")
        self.instance = "aa001122"
        self.area = self.backups.instances / self.instance
        self.area.mkdir(parents=True)
        self.retired = self.area / "retired.json"
        self.backups.is_gone = Mock(return_value=True)

    def test_expiry_requires_separate_confirmations_and_fresh_listing(self):
        self.backups.reconcile({}, 100.0)
        self.backups.reconcile({}, 101.0)
        self.assertEqual(read_state(self.retired), {"first_missing": 100.0})
        then = 100.0 + CONFIRM_MISSING_AFTER
        self.backups.reconcile({}, then)
        self.assertTrue(self.area.is_dir())
        self.assertEqual(read_state(self.retired), {"confirmed_at": then})
        self.backups.reconcile({}, then + RETIRE_AFTER - 1)
        self.assertTrue(self.area.is_dir())

        self.backups.account.billing_instances = Mock(return_value={self.instance: {}})
        self.backups.reconcile({}, then + RETIRE_AFTER)
        self.assertTrue(self.area.is_dir())
        self.backups.account.billing_instances.assert_called_once_with(strict=True)

        self.backups.account.billing_instances.return_value = {}
        self.backups.reconcile({}, then + RETIRE_AFTER)
        self.assertFalse(self.area.exists())

    def test_reappearing_instance_cancels_retirement(self):
        self.backups.reconcile({}, 100.0)
        self.backups.reconcile({self.instance: {}}, 101.0)
        self.assertTrue(self.area.is_dir())
        self.assertFalse(self.retired.exists())

    def test_panel_confirms_instance_still_exists(self):
        self.backups.is_gone.return_value = False
        self.backups.reconcile({}, 100.0)
        self.assertTrue(self.area.is_dir())
        self.assertFalse(self.retired.exists())
