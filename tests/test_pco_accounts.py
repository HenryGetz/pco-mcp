"""pco_accounts tests: credential resolution, defaults, routing, clients, fan-out.

No network access. All credential/config files are temporary and every test
isolates PCO_* environment variables.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

import pco_accounts as pa


CREDS = {
    "unw": {"appId": "unw-id", "appSecret": "unw-secret", "orgName": "Main Campus"},
    "rs": {"appId": "rs-id", "appSecret": "rs-secret", "orgName": "North Campus"},
}


class AccountsTestBase(unittest.TestCase):
    """Point pco_accounts at temp files and clear PCO_* environment variables."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.creds_path = root / "creds.json"
        self.config_path = root / "config.json"

        self._file_patches = []
        for name, value in (("CREDS_FILE", self.creds_path), ("CONFIG_FILE", self.config_path)):
            patcher = mock.patch.object(pa, name, value)
            patcher.start()
            self._file_patches.append(patcher)

        self._env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patcher.start()
        for key in [k for k in os.environ if k.startswith("PCO_")]:
            os.environ.pop(key, None)

        # Unit tests are direct stdio: never read/write a real chat runtime file.
        scope_patcher = mock.patch.object(pa, "_chat_scope", return_value=None)
        scope_patcher.start()
        self._file_patches.append(scope_patcher)

        pa.reset_process_state()
        self.addCleanup(self._teardown)

    def _teardown(self):
        # Clears process-local selection, the installation snapshot, and caches.
        pa.reset_process_state()
        for patcher in reversed(self._file_patches):
            patcher.stop()
        self._env_patcher.stop()

    def write_creds(self, data):
        self.creds_path.write_text(json.dumps(data), encoding="utf-8")

    def write_config(self, data):
        self.config_path.write_text(json.dumps(data), encoding="utf-8")


class DefaultResolutionTests(AccountsTestBase):
    def test_first_creds_key_is_default(self):
        self.write_creds(CREDS)
        self.assertEqual(pa.list_accounts(), ["unw", "rs"])
        self.assertEqual(pa.default_account(), "unw")
        self.assertEqual(pa.resolve_accounts(None), ["unw"])
        self.assertEqual(pa.resolve_accounts(""), ["unw"])

    def test_legacy_single_account_file_is_default(self):
        self.write_creds({"legacy": {"appId": "legacy-id", "appSecret": "legacy-secret"}})
        self.assertEqual(pa.list_accounts(), ["legacy"])
        self.assertEqual(pa.default_account(), "legacy")
        self.assertEqual(pa.resolve_accounts(None), ["legacy"])
        self.assertEqual(pa.credentials("legacy"), ("legacy-id", "legacy-secret"))
        self.assertEqual(pa.org_name("legacy"), "legacy")

    def test_config_default_beats_first_file_key(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "rs"})
        self.assertEqual(pa.default_account(), "rs")
        self.assertEqual(pa.resolve_accounts(None), ["rs"])

    def test_env_default_beats_config_and_file(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "unw"})
        os.environ["PCO_DEFAULT_ACCOUNT"] = "rs"
        self.assertEqual(pa.default_account(), "rs")
        self.assertEqual(pa.resolve_accounts(None), ["rs"])

    def test_invalid_env_default_raises_and_names_valid_accounts(self):
        self.write_creds(CREDS)
        os.environ["PCO_DEFAULT_ACCOUNT"] = "bogus"
        with self.assertRaises(pa.AccountError) as ctx:
            pa.default_account()
        message = str(ctx.exception)
        self.assertIn("bogus", message)
        self.assertIn("unw", message)
        self.assertIn("rs", message)

    def test_invalid_config_default_raises(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "bogus"})
        with self.assertRaises(pa.AccountError) as ctx:
            pa.default_account()
        self.assertIn("unw", str(ctx.exception))

    def test_no_configuration_falls_back_to_unw(self):
        self.assertEqual(pa.list_accounts(), [])
        self.assertEqual(pa.default_account(), "unw")
        with self.assertRaises(pa.AccountError):
            pa.resolve_accounts("all")


class EnvOnlyTests(AccountsTestBase):
    def test_env_only_is_synthetic_env_account(self):
        os.environ["PCO_APP_ID"] = "env-id"
        os.environ["PCO_SECRET"] = "env-secret"

        self.assertEqual(pa.list_accounts(), ["env"])
        self.assertEqual(pa.default_account(), "env")
        self.assertEqual(pa.resolve_accounts(None), ["env"])
        self.assertEqual(pa.credentials("env"), ("env-id", "env-secret"))

    def test_env_only_serves_any_requested_name(self):
        os.environ["PCO_APP_ID"] = "env-id"
        os.environ["PCO_SECRET"] = "env-secret"

        self.assertEqual(pa.resolve_accounts("unw"), ["unw"])
        self.assertEqual(pa.credentials("unw"), ("env-id", "env-secret"))
        self.assertEqual(pa.org_name("unw"), "unw")

    def test_env_app_secret_alias(self):
        os.environ["PCO_APP_ID"] = "env-id"
        os.environ["PCO_APP_SECRET"] = "alias-secret"

        self.assertEqual(pa.credentials("env"), ("env-id", "alias-secret"))

    def test_named_env_account_with_creds_file(self):
        self.write_creds(CREDS)
        os.environ["PCO_APP_ID"] = "env-id"
        os.environ["PCO_SECRET"] = "env-secret"

        self.assertEqual(pa.credentials("env"), ("env-id", "env-secret"))
        # A named credentials-file entry always wins over the env pair.
        self.assertEqual(pa.credentials("rs"), ("rs-id", "rs-secret"))

    def test_all_excludes_env_when_creds_file_has_accounts(self):
        self.write_creds(CREDS)
        os.environ["PCO_APP_ID"] = "env-id"
        os.environ["PCO_SECRET"] = "env-secret"

        # A stray env pair must not add a duplicate org to account="all".
        self.assertEqual(pa.list_accounts(), ["unw", "rs"])
        self.assertEqual(pa.resolve_accounts("all"), ["unw", "rs"])
        # It is still reachable explicitly.
        self.assertEqual(pa.credentials("env"), ("env-id", "env-secret"))


class RoutingTests(AccountsTestBase):
    def test_resolve_all_is_case_insensitive(self):
        self.write_creds(CREDS)
        self.assertTrue(pa.is_all("all"))
        self.assertTrue(pa.is_all("ALL"))
        self.assertFalse(pa.is_all(None))
        self.assertFalse(pa.is_all("rs"))
        self.assertEqual(pa.resolve_accounts("all"), ["unw", "rs"])
        self.assertEqual(pa.resolve_accounts("ALL"), ["unw", "rs"])
        self.assertEqual(pa.resolve_accounts("rs"), ["rs"])

    def test_unknown_explicit_account_raises(self):
        self.write_creds(CREDS)
        with self.assertRaises(pa.UnknownAccountError) as ctx:
            pa.resolve_accounts("bogus")
        message = str(ctx.exception)
        self.assertIn("bogus", message)
        self.assertIn("unw", message)
        self.assertIn("rs", message)

    def test_credentials_missing_secret_raises(self):
        self.write_creds({"broken": {"appId": "only-id"}})
        with self.assertRaises(pa.AccountError):
            pa.credentials("broken")

    def test_org_name_precedence(self):
        self.write_creds({
            "unw": {"appId": "a", "appSecret": "b", "orgName": "Creds Org"},
            "rs": {"appId": "c", "appSecret": "d", "org_name": "Snake Org"},
            "revive": {"appId": "e", "appSecret": "f"},
        })
        self.write_config({"orgNames": {"revive": "Config Org", "rs": "Config Should Lose"}})

        self.assertEqual(pa.org_name("unw"), "Creds Org")
        self.assertEqual(pa.org_name("rs"), "Snake Org")
        self.assertEqual(pa.org_name("revive"), "Config Org")

    def test_account_meta_marks_default(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "rs"})

        rs = pa.account_meta("rs")
        self.assertEqual(rs, {"account": "rs", "org_name": "North Campus", "default": True})
        unw = pa.account_meta("unw")
        self.assertEqual(unw["account"], "unw")
        self.assertEqual(unw["org_name"], "Main Campus")
        self.assertFalse(unw["default"])


class FakeHttpxClient:
    """Stand-in for httpx.Client that records constructor kwargs."""

    instances: list["FakeHttpxClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers = dict(kwargs.get("headers") or {})
        self.closed = False
        FakeHttpxClient.instances.append(self)

    def close(self):
        self.closed = True


class ClientCacheTests(AccountsTestBase):
    def _patch_httpx_client(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(httpx, "Client", FakeHttpxClient))
        if hasattr(pa, "Client"):
            stack.enter_context(mock.patch.object(pa, "Client", FakeHttpxClient))
        return stack

    def test_client_per_account_isolated_and_cached(self):
        self.write_creds(CREDS)
        FakeHttpxClient.instances = []
        self._patch_httpx_client()

        unw_first = pa.get_client("unw")
        unw_second = pa.get_client("unw")
        rs_client = pa.get_client("rs")

        self.assertIs(unw_first, unw_second)
        self.assertIsNot(unw_first, rs_client)
        self.assertEqual(len(FakeHttpxClient.instances), 2)

        expected_unw = "Basic " + base64.b64encode(b"unw-id:unw-secret").decode()
        expected_rs = "Basic " + base64.b64encode(b"rs-id:rs-secret").decode()
        self.assertEqual(unw_first.headers["Authorization"], expected_unw)
        self.assertEqual(rs_client.headers["Authorization"], expected_rs)
        self.assertEqual(unw_first.kwargs.get("base_url"), pa.PCO_BASE_URL)

    def test_reset_cache_closes_and_rebuilds_clients(self):
        self.write_creds(CREDS)
        FakeHttpxClient.instances = []
        self._patch_httpx_client()

        first = pa.get_client("unw")
        pa.reset_cache()
        second = pa.get_client("unw")

        self.assertIsNot(first, second)
        self.assertTrue(first.closed)


class FanOutTests(AccountsTestBase):
    def test_partial_failure_keeps_other_results(self):
        self.write_creds(CREDS)

        def call(account):
            if account == "rs":
                raise pa.AccountError("rs auth failed")
            return {"account": account, "payload": 1}

        envelope = pa.fan_out(["unw", "rs"], call)

        self.assertEqual(envelope["account"], "all")
        self.assertEqual(envelope["accounts"], ["unw", "rs"])
        self.assertEqual([row["account"] for row in envelope["results"]], ["unw"])
        self.assertEqual(envelope["results"][0]["org_name"], "Main Campus")
        self.assertEqual(envelope["results"][0]["data"], {"account": "unw", "payload": 1})
        self.assertEqual(len(envelope["errors"]), 1)
        self.assertEqual(envelope["errors"][0]["account"], "rs")
        self.assertEqual(envelope["errors"][0]["org_name"], "North Campus")
        self.assertIn("rs auth failed", str(envelope["errors"][0]["error"]))

    def test_all_failures_raise(self):
        self.write_creds(CREDS)

        def call(account):
            raise pa.AccountError(f"{account} down")

        with self.assertRaises(RuntimeError) as ctx:
            pa.fan_out(["unw", "rs"], call)
        message = str(ctx.exception)
        self.assertIn("unw down", message)
        self.assertIn("rs down", message)


class ConfigWriteTests(AccountsTestBase):
    def test_set_default_account_writes_config_file_only(self):
        self.write_creds(CREDS)
        self.write_config({"orgNames": {"unw": "Main Campus"}})
        creds_before = self.creds_path.read_text(encoding="utf-8")

        path = pa.set_default_account("rs")

        self.assertEqual(str(path), str(self.config_path))
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["defaultAccount"], "rs")
        self.assertEqual(config["orgNames"], {"unw": "Main Campus"})
        self.assertEqual(self.creds_path.read_text(encoding="utf-8"), creds_before)
        self.assertEqual(pa.default_account(), "rs")

    def test_set_default_account_rejects_all(self):
        self.write_creds(CREDS)
        with self.assertRaises(pa.AccountError):
            pa.set_default_account("all")
        self.assertFalse(self.config_path.exists())


class DefaultSourceTests(AccountsTestBase):
    """default_account_with_source() labels each rung of the precedence ladder."""

    def test_first_key_source(self):
        self.write_creds(CREDS)
        self.assertEqual(pa.default_account_with_source(), ("unw", "first-key"))
        self.assertEqual(pa.default_account(), "unw")

    def test_config_source(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "rs"})
        self.assertEqual(pa.default_account_with_source(), ("rs", "config"))

    def test_env_source(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "unw"})
        os.environ["PCO_DEFAULT_ACCOUNT"] = "rs"
        self.assertEqual(
            pa.default_account_with_source(), ("rs", "env:PCO_DEFAULT_ACCOUNT")
        )

    def test_env_credentials_source(self):
        os.environ["PCO_APP_ID"] = "env-id"
        os.environ["PCO_SECRET"] = "env-secret"
        self.assertEqual(
            pa.default_account_with_source(), ("env", "env-credentials")
        )

    def test_builtin_source(self):
        self.assertEqual(pa.list_accounts(), [])
        self.assertEqual(pa.default_account_with_source(), ("unw", "builtin"))

    def test_invalid_env_default_still_raises(self):
        self.write_creds(CREDS)
        os.environ["PCO_DEFAULT_ACCOUNT"] = "bogus"
        with self.assertRaises(pa.AccountError) as ctx:
            pa.default_account_with_source()
        self.assertIn("bogus", str(ctx.exception))


class InstallationSnapshotTests(AccountsTestBase):
    def test_snapshot_is_cached_until_reset(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "unw"})
        self.assertEqual(pa.installation_default(), ("unw", "config"))

        # A later config change must not move this process's installation default.
        self.write_config({"defaultAccount": "rs"})
        self.assertEqual(pa.installation_default(), ("unw", "config"))
        # Dynamic resolution (a new process would see this) does follow the file.
        self.assertEqual(pa.default_account(), "rs")

        pa.reset_process_state()
        self.assertEqual(pa.installation_default(), ("rs", "config"))

    def test_snapshot_ignores_later_env_change(self):
        self.write_creds(CREDS)
        self.assertEqual(pa.installation_default(), ("unw", "first-key"))

        os.environ["PCO_DEFAULT_ACCOUNT"] = "rs"
        self.assertEqual(pa.installation_default(), ("unw", "first-key"))
        self.assertEqual(pa.default_account(), "rs")

    def test_snapshot_ignores_later_creds_change(self):
        self.write_creds({"unw": CREDS["unw"]})
        self.assertEqual(pa.installation_default(), ("unw", "first-key"))

        # Add an account that would sort first: the snapshot is already taken.
        self.write_creds({"aaa": {"appId": "a", "appSecret": "b"}, "unw": CREDS["unw"]})
        self.assertEqual(pa.installation_default(), ("unw", "first-key"))
        pa.reset_process_state()
        self.assertEqual(pa.installation_default(), ("aaa", "first-key"))


class SelectionTests(AccountsTestBase):
    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)

    def test_select_stores_without_changing_installation_default(self):
        self.assertIsNone(pa.selected_account())
        self.assertEqual(pa.select_account("rs"), "rs")
        self.assertEqual(pa.selected_account(), "rs")
        self.assertEqual(pa.default_account(), "unw")
        self.assertEqual(pa.select_account("unw"), "unw")
        self.assertEqual(pa.selected_account(), "unw")

    def test_repeat_selection_is_stable(self):
        pa.select_account("rs")
        self.assertEqual(pa.select_account("rs"), "rs")
        self.assertEqual(pa.selected_account(), "rs")

    def test_none_and_blank_clear_selection(self):
        for clear_value in (None, "", "   "):
            pa.select_account("rs")
            self.assertIsNone(pa.select_account(clear_value))
            self.assertIsNone(pa.selected_account())

    def test_all_is_rejected(self):
        with self.assertRaises(pa.AccountError):
            pa.select_account("all")
        self.assertIsNone(pa.selected_account())

    def test_unknown_account_rejected_with_valid_names(self):
        with self.assertRaises(pa.AccountError) as ctx:
            pa.select_account("bogus")
        message = str(ctx.exception)
        self.assertIn("bogus", message)
        self.assertIn("unw", message)
        self.assertIn("rs", message)
        self.assertIsNone(pa.selected_account())

    def test_account_without_usable_credentials_is_rejected(self):
        self.write_creds({"broken": {"appId": "only-id"}})
        with self.assertRaises(pa.AccountError):
            pa.select_account("broken")
        self.assertIsNone(pa.selected_account())

    def test_selection_validation_reads_the_current_file(self):
        self.assertEqual(pa.select_account("rs"), "rs")
        self.write_creds({"unw": CREDS["unw"]})
        with self.assertRaises(pa.AccountError):
            pa.select_account("rs")

    def test_legacy_env_only_selection_allows_any_name(self):
        self.creds_path.unlink()
        os.environ["PCO_APP_ID"] = "env-id"
        os.environ["PCO_SECRET"] = "env-secret"
        self.assertEqual(pa.select_account("unw"), "unw")
        self.assertEqual(pa.selected_account(), "unw")

    def test_reset_process_state_clears_selection_and_snapshot(self):
        pa.select_account("rs")
        self.assertEqual(pa.installation_default(), ("unw", "first-key"))

        pa.reset_process_state()

        self.assertIsNone(pa.selected_account())
        self.write_config({"defaultAccount": "rs"})
        self.assertEqual(pa.installation_default(), ("rs", "config"))


class ChatStateFileTests(AccountsTestBase):
    """The per-chat runtime file that survives lazy-tool's per-call pco children."""

    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)
        self.state_root = Path(self._tmp.name) / "runtime"
        self.state_root.mkdir()
        self._xdg = mock.patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": str(self.state_root)}, clear=False
        )
        self._xdg.start()
        self.addCleanup(self._xdg.stop)
        self._scope = mock.patch.object(
            pa, "_chat_scope", return_value=(4242, 777)
        )
        self._scope.start()
        self.addCleanup(self._scope.stop)
        self.state_path = self.state_root / "pco-mcp" / "chat-4242-777.json"

    def reset_in_memory_selection(self):
        with pa._process_lock:
            pa._selected_account = None

    def test_selection_survives_a_new_process_with_secure_permissions(self):
        self.assertEqual(pa.chat_scope_id(), "4242:777")
        self.assertEqual(pa.select_account("rs"), "rs")

        self.assertTrue(self.state_path.is_file())
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_path.parent.stat().st_mode), 0o700)
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["account"], "rs")
        self.assertEqual(payload["scope_pid"], 4242)
        self.assertEqual(payload["scope_starttime"], 777)

        # Simulate the next lazy-tool call: a brand-new pco process.
        self.reset_in_memory_selection()
        self.assertEqual(pa.selected_account(), "rs")

        pa.select_account(None)
        self.assertFalse(self.state_path.exists())
        self.assertIsNone(pa.selected_account())

    def test_scopes_are_isolated(self):
        pa.select_account("unw")

        with mock.patch.object(pa, "_chat_scope", return_value=(4243, 888)):
            self.reset_in_memory_selection()
            self.assertIsNone(pa.selected_account())
            self.assertEqual(pa.select_account("rs"), "rs")
            self.reset_in_memory_selection()
            self.assertEqual(pa.selected_account(), "rs")

        self.reset_in_memory_selection()
        self.assertEqual(pa.selected_account(), "unw")
        self.assertTrue((self.state_root / "pco-mcp" / "chat-4243-888.json").is_file())

    def test_malformed_state_file_is_ignored(self):
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text("{not json", encoding="utf-8")
        self.reset_in_memory_selection()
        self.assertIsNone(pa.selected_account())

    def test_direct_stdio_writes_no_chat_file(self):
        with mock.patch.object(pa, "_chat_scope", return_value=None):
            self.assertEqual(pa.select_account("rs"), "rs")
            self.assertEqual(pa.selected_account(), "rs")
        self.assertFalse((self.state_root / "pco-mcp").exists())


if __name__ == "__main__":
    unittest.main()
