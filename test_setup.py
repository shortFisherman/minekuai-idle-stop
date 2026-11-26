import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from authorize import parse_session, replace_auth
from minekuai import AuthError
from monitor import load_config


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.directory = Path(self.tempdir.name)

    def test_config_uses_production_defaults(self):
        config = self.directory / "config.json"
        config.write_text('{"instance":"0123abcd","host":"node.example.net","port":25565}', encoding="utf-8")
        result = load_config(config)
        self.assertEqual((result.idle_seconds, result.poll_seconds), (600, 30))

    def test_invalid_port_and_query_interval(self):
        config = self.directory / "config.json"
        for invalid in ({"port": True}, {"poll_seconds": 601}, {"host": "two words"}):
            values = {"instance": "0123abcd", "host": "node.example.net", "port": 25565, **invalid}
            config.write_text(json.dumps(values), encoding="utf-8")
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                load_config(config)

    def test_cookie_client_id_and_url_encoding(self):
        payload = base64.urlsafe_b64encode(b'{"clientid":"demo-client"}').decode().rstrip("=")
        token = f"header.{payload}.signature"
        raw = quote("Bearer " + token)
        self.assertEqual(parse_session(raw), {"token": token, "clientid": "demo-client"})

    def test_token_without_client_claim_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_session("header.e30.signature")

    def test_replace_keeps_old_credential_when_verification_fails(self):
        current = self.directory / "auth.json"
        current.write_text("old", encoding="utf-8")
        with patch("authorize.Minekuai") as provider:
            provider.return_value.billing_cycle.side_effect = AuthError("expired")
            with self.assertRaises(AuthError):
                replace_auth("0123abcd", current, {"token": "example", "clientid": "demo-client"})
        self.assertEqual(current.read_text(encoding="utf-8"), "old")
        self.assertEqual(list(self.directory.iterdir()), [current])

    def test_replace_writes_validated_credential(self):
        current = self.directory / "auth.json"
        session = {"token": "example", "clientid": "demo-client"}
        with patch("authorize.Minekuai") as provider:
            provider.return_value.billing_cycle.return_value = None
            replace_auth("0123abcd", current, session)
        self.assertEqual(json.loads(current.read_text(encoding="utf-8")), session)
        if os.name == "posix":
            self.assertEqual(current.stat().st_mode & 0o777, 0o600)
