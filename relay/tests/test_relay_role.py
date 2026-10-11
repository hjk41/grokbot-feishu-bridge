#!/usr/bin/env python3
"""Smoke tests for RELAY_ROLE parsing and CLI path overrides (no live Feishu)."""
import os
import sys
import unittest
from pathlib import Path

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DIR)

import relay  # noqa: E402
import progress  # noqa: E402


class RelayRoleTests(unittest.TestCase):
    def test_parse_default_full(self):
        self.assertEqual(relay.parse_relay_role(env={}), "full")
        self.assertEqual(relay.parse_relay_role("", env={}), "full")
        self.assertEqual(relay.parse_relay_role(None, env={"RELAY_ROLE": "full"}), "full")

    def test_parse_inbound_and_cloud_worker(self):
        self.assertEqual(relay.parse_relay_role("inbound"), "inbound")
        self.assertEqual(relay.parse_relay_role("INBOUND"), "inbound")
        self.assertEqual(relay.parse_relay_role("cloud-worker"), "cloud-worker")
        self.assertEqual(relay.parse_relay_role("CLOUD-WORKER"), "cloud-worker")

    def test_parse_invalid_fails_closed(self):
        with self.assertRaises(ValueError):
            relay.parse_relay_role("worker")
        with self.assertRaises(ValueError):
            relay.parse_relay_role("nas")

    def test_role_gates(self):
        self.assertTrue(relay.role_starts_message_consumer("full"))
        self.assertTrue(relay.role_starts_message_consumer("inbound"))
        self.assertFalse(relay.role_starts_message_consumer("cloud-worker"))
        self.assertFalse(relay.role_starts_card_consumer("cloud-worker"))
        self.assertFalse(relay.role_starts_bus_refresh("cloud-worker"))
        self.assertTrue(relay.role_starts_bus_refresh("inbound"))


class CliPathTests(unittest.TestCase):
    def test_feishu_cli_prefers_FEISHU_CLI(self):
        self.assertEqual(
            relay.resolve_feishu_cli({"FEISHU_CLI": "/opt/feishu", "FEISHU_BIN": "/opt/old"}),
            "/opt/feishu",
        )

    def test_feishu_cli_falls_back_FEISHU_BIN(self):
        self.assertEqual(relay.resolve_feishu_cli({"FEISHU_BIN": "/opt/bin"}), "/opt/bin")

    def test_feishu_cli_default(self):
        self.assertEqual(relay.resolve_feishu_cli({}), relay.DEFAULT_FEISHU_CLI)

    def test_lark_cli(self):
        self.assertEqual(relay.resolve_lark_cli({"LARK_CLI": "/opt/lark"}), "/opt/lark")
        self.assertEqual(relay.resolve_lark_cli({}), relay.DEFAULT_LARK_CLI)

    def test_progress_respects_FEISHU_CLI(self):
        old = os.environ.get("FEISHU_CLI")
        old_bin = os.environ.get("FEISHU_BIN")
        try:
            os.environ.pop("FEISHU_BIN", None)
            os.environ["FEISHU_CLI"] = "/tmp/fake-feishu-cli"
            self.assertEqual(progress._resolve_feishu_bin(), "/tmp/fake-feishu-cli")
        finally:
            if old is None:
                os.environ.pop("FEISHU_CLI", None)
            else:
                os.environ["FEISHU_CLI"] = old
            if old_bin is None:
                os.environ.pop("FEISHU_BIN", None)
            else:
                os.environ["FEISHU_BIN"] = old_bin


if __name__ == "__main__":
    unittest.main()
