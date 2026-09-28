import base64
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import metadata
import watch


def write_auth(home, email, account_id="personal", until="2026-10-10T15:19:46Z", checked="2026-09-21T19:32:00Z"):
    home.mkdir(parents=True, exist_ok=True)
    payload = {"email": email, "exp": 9999999999, "https://api.openai.com/auth": {
        "chatgpt_account_id": account_id, "chatgpt_plan_type": "pro",
        "chatgpt_subscription_active_until": until,
        "chatgpt_subscription_last_checked": checked,
    }}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": "fake." + encoded + ".fake", "account_id": account_id}}))


def usage(email, pct=25):
    return {"account": email, "usage": {"accountEmail": email,
            "updatedAt": "2026-09-28T03:00:00Z",
            "secondary": {"usedPercent": pct, "resetsAt": "2026-10-03T17:00:00Z"}}}


class Accounts(unittest.TestCase):
    def test_orca_union_is_deduplicated_by_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, patch("pathlib.Path.home", return_value=Path(tmp)), patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            write_auth(root / ".codex", "first@example.com")
            orca = root / "Library/Application Support/orca/codex-accounts"
            write_auth(orca / "a/home", "first@example.com")
            write_auth(orca / "b/home", "second@example.com")
            write_auth(orca / "c/home", "first@example.com", account_id="team")
            items, homes = metadata.discover_codex()
            self.assertEqual(len(items), 3)
            self.assertEqual(set(homes), {str(orca / "b/home"), str(orca / "c/home")})
            self.assertEqual(items[0]["sources"], ["System", "Orca"])

    def test_auth_expiry_is_not_subscription_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_auth(Path(tmp), "a@example.com", until=None)
            self.assertIsNone(metadata.codex_identity(tmp)["subscription"])

    def test_workspace_mismatch_does_not_get_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_auth(Path(tmp), "a@example.com")
            result = metadata.codex_identity(tmp, {"email": "a@example.com", "workspaceAccountID": "other"})
            self.assertIsNone(result["subscription"])

    def test_newest_checked_date_wins(self):
        with tempfile.TemporaryDirectory() as tmp, patch("pathlib.Path.home", return_value=Path(tmp)), patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            write_auth(root / ".codex", "a@example.com", checked="2026-09-28T00:00:00Z")
            write_auth(root / "Library/Application Support/orca/codex-accounts/a/home", "a@example.com", until="2026-10-01T00:00:00Z")
            items, homes = metadata.discover_codex()
            self.assertEqual(items[0]["subscription"]["until"], "2026-10-10T15:19:46Z")
            self.assertEqual(homes, [])

    def test_partial_failure_keeps_success_and_failed_account(self):
        payload = [usage("a@example.com"), {"account": "b@example.com", "error": {"message": "401 unauthorized"}}]
        with patch.object(watch, "discover_codex", return_value=([], [])), patch.object(watch, "run", return_value=json.dumps(payload)):
            accounts = watch.fetch_codex()
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["windows"][0]["pct"], 25)
        self.assertEqual(accounts[1]["error"], "нужен повторный вход")

    def test_nonzero_process_can_still_return_account_json(self):
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, '[{"account":"b@example.com"}]', "error")):
            self.assertEqual(json.loads(watch.run(["test"]))[0]["account"], "b@example.com")

    def test_unreported_local_account_stays_visible(self):
        inventory = [{"email": "b@example.com", "account_id": "b", "subscription": None}]
        with patch.object(watch, "discover_codex", return_value=(inventory, [])), patch.object(watch, "run", return_value=json.dumps([usage("a@example.com")])):
            accounts = watch.fetch_codex()
        self.assertEqual([a["email"] for a in accounts], ["a@example.com", "b@example.com"])
        self.assertTrue(accounts[1]["error"])

    def test_same_email_workspaces_do_not_borrow_billing_dates(self):
        inventory = [{"email": "a@example.com", "account_id": k, "subscription": {"until": "2026-10-10"}} for k in ("a", "b")]
        with patch.object(watch, "discover_codex", return_value=(inventory, [])), patch.object(watch, "run", return_value=json.dumps([usage("a@example.com")])):
            accounts = watch.fetch_codex()
        self.assertIsNone(accounts[0].get("subscription"))

    def test_last_good_usage_preserves_original_timestamp(self):
        old = [{"email": "a@example.com", "windows": [{"pct": 25}], "fetched_at": "2026-09-20T00:00:00Z"}]
        new = [{"email": "a@example.com", "windows": [], "error": "offline"}]
        result = watch.merge_last_good(old, new)[0]
        self.assertEqual(result["fetched_at"], old[0]["fetched_at"])
        self.assertEqual(result["windows"], old[0]["windows"])
        self.assertEqual(result["error"], "offline")

    def test_orca_paths_passed_only_in_temporary_config(self):
        def probe(cmd, timeout, env):
            config = Path(env["CODEXBAR_CONFIG"])
            self.assertEqual(json.loads(config.read_text())["providers"][0]["codexProfileHomePaths"], ["/test/orca/home"])
            self.assertIn("--all-accounts", cmd)
            self.assertEqual(cmd[cmd.index("--source") + 1], "oauth")
            return json.dumps([usage("a@example.com")])
        with patch.object(watch, "discover_codex", return_value=([], ["/test/orca/home"])), patch.object(watch, "run", side_effect=probe):
            watch.fetch_codex()

    def test_bad_window_does_not_hide_another_account(self):
        payload = [usage("a@example.com", "NaN"), usage("b@example.com", 10)]
        with patch.object(watch, "discover_codex", return_value=([], [])), patch.object(watch, "run", return_value=json.dumps(payload)):
            accounts = watch.fetch_codex()
        self.assertEqual(accounts[0]["windows"], [])
        self.assertTrue(accounts[0]["error"])
        self.assertEqual(accounts[1]["windows"][0]["pct"], 10)


class Subscriptions(unittest.TestCase):
    def test_confirmed_dates_roundtrip_without_monthly_rollover(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LIMITS_SUBSCRIPTIONS": tmp + "/dates.json"}):
            metadata.save_subscription("claude", "A@example.com", "2026-10-07", renews=True, source="billing")
            entry = metadata.load_subscriptions()["claude:a@example.com"]
            text = watch.subscription_text(entry, now=datetime(2026, 11, 1, tzinfo=timezone.utc))
            self.assertIn("07.10.2026", text)
            self.assertIn("дата прошла", text)
            self.assertEqual(os.stat(tmp + "/dates.json").st_mode & 0o777, 0o600)

    def test_corrupt_dates_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LIMITS_SUBSCRIPTIONS": tmp + "/dates.json"}):
            path = Path(tmp) / "dates.json"
            path.write_text("broken")
            with self.assertRaises(ValueError):
                metadata.save_subscription("claude", "a@example.com", "2026-10-07")
            self.assertEqual(path.read_text(), "broken")

    def test_date_only_is_not_shifted_by_timezone(self):
        text = watch.subscription_text({"until": "2026-10-07", "kind": "renews"}, now=datetime(2026, 9, 28, tzinfo=timezone.utc))
        self.assertIn("07.10.2026", text)
        self.assertNotIn("00:00", text)

    def test_unknown_does_not_infer_from_creation_or_limit_reset(self):
        self.assertEqual(watch.subscription_text({"createdAt": "2026-09-10", "resetsAt": "2026-10-10"}), "подписка: дата неизвестна")


class Display(unittest.TestCase):
    def test_ansi_clipping_does_not_break_escape_sequences(self):
        line = watch.BOLD + "x" * 200 + watch.RESET
        clipped = watch.clip_line(line, 79)
        self.assertEqual(watch.ANSI.sub("", clipped), "x" * 79)
        self.assertTrue(clipped.endswith(watch.RESET))

    def test_five_accounts_fit_120_by_24(self):
        window = {"pct": 20, "label": "7d", "resets_at": "2026-10-03T17:00:00Z", "clock": "03.10 20:00", "ahead": True}
        claude = [dict(provider="claude", email=f"claude{i}@example.com", active=False, windows=[window]*3) for i in range(3)]
        codex = [dict(provider="codex", email=f"codex{i}@example.com", active=False, windows=[window]) for i in range(2)]
        with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((120, 24))):
            text = watch.ANSI.sub("", watch.render(claude, codex, 0, 0, []))
        self.assertLessEqual(len(text.splitlines()), 23)
        self.assertTrue(all(len(line) <= 119 for line in text.splitlines()))
        self.assertIn("codex1@example.com", text)


if __name__ == "__main__":
    unittest.main()
