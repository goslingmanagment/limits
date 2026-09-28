import base64
import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_last_good_usage_fills_in_when_cswap_cannot_fetch(self):
        payload = {"accounts": [{"email": "a@example.com", "number": 1, "usageStatus": "unavailable", "usage": None,
                                 "lastGoodUsage": {"sevenDay": {"pct": 40, "resetsAt": "2026-10-01T14:00:00Z"}},
                                 "lastGoodFetchedAt": "2026-09-28T03:00:00Z"}]}
        with patch.object(watch, "claude_plans", return_value={}), patch.object(watch, "run", return_value=json.dumps(payload)):
            account = watch.fetch_claude()[0]
        self.assertEqual([(w["label"], w["pct"]) for w in account["windows"]], [("7d", 40.0)])
        self.assertEqual(account["fetched_at"], "2026-09-28T03:00:00Z")
        notice = watch.notice_text(account, None, datetime(2026, 9, 28, 3, 20, tzinfo=timezone.utc))
        self.assertEqual(notice, "не удалось обновить · последние данные 20m назад")

    def test_cswap_error_is_reported_in_its_own_words(self):
        envelope = {"schemaVersion": 1, "error": {"type": "ClaudeSwitchError", "message": "No accounts configured"}}
        for out, message in ((json.dumps(envelope), "claude-swap: No accounts configured"),
                             ("", "claude-swap не отвечает (cswap list --json)")):
            with patch.object(watch, "run", return_value=out), self.assertRaises(watch.FetchError) as caught:
                watch.fetch_claude()
            self.assertEqual(str(caught.exception), message)

    def test_passed_reset_shows_the_window_as_it_is_now(self):
        now = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
        passed = (now - timedelta(hours=1)).isoformat()
        week = {"label": "7d", "pct": 99, "resets_at": passed, "runs_out_at": passed, "ahead": True, "clock": "x"}
        five = {"label": "5h", "pct": 70, "resets_at": passed}
        later = {"label": "Fable", "pct": 35, "resets_at": (now + timedelta(days=1)).isoformat()}
        claude = watch.roll_over([dict(provider="claude", windows=[week, five, later])], now)[0]["windows"]
        self.assertEqual((claude[0]["pct"], claude[0]["runs_out_at"], claude[0]["ahead"]), (0.0, None, False))
        self.assertEqual(watch.parse_date(claude[0]["resets_at"]), now - timedelta(hours=1) + timedelta(days=7))
        self.assertEqual((claude[1]["pct"], claude[1]["resets_at"]), (0.0, None))
        self.assertIs(claude[2], later)
        codex = watch.roll_over([dict(provider="codex", windows=[week])], now)[0]["windows"][0]
        self.assertEqual((codex["pct"], codex["resets_at"]), (0.0, None))
        self.assertIsNone(watch.roll_over(None, now))
        self.assertEqual(week["pct"], 99)

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
            text, warn = watch.subscription_text(entry, now=datetime(2026, 11, 1, tzinfo=timezone.utc))
            self.assertEqual(text, "продление 07.10 — проверь")
            self.assertTrue(warn)
            self.assertEqual(os.stat(tmp + "/dates.json").st_mode & 0o777, 0o600)

    def test_corrupt_dates_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LIMITS_SUBSCRIPTIONS": tmp + "/dates.json"}):
            path = Path(tmp) / "dates.json"
            path.write_text("broken")
            with self.assertRaises(ValueError):
                metadata.save_subscription("claude", "a@example.com", "2026-10-07")
            self.assertEqual(path.read_text(), "broken")

    def test_date_only_is_not_shifted_by_timezone(self):
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        try:
            with patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
                time.tzset()
                text = watch.subscription_text({"until": "2026-10-07", "kind": "renews"}, now)
        finally:
            time.tzset()
        self.assertEqual(text, ("продление 07.10", False))

    def test_other_year_and_end_of_period_are_spelled_out(self):
        text, warn = watch.subscription_text({"until": "2027-01-15T10:00:00Z"}, datetime(2026, 9, 28, tzinfo=timezone.utc))
        self.assertEqual((text, warn), ("оплачено до 15.01.2027", False))

    def test_dates_in_the_next_five_days_are_flagged(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        def text(until):
            return watch.subscription_text({"until": until, "kind": "renews"}, now)
        self.assertEqual(text("2026-09-28"), ("продление 28.09 (сегодня)", True))
        self.assertEqual(text("2026-09-29"), ("продление 29.09 (завтра)", True))
        self.assertEqual(text("2026-10-03"), ("продление 03.10 (5 дн.)", True))
        self.assertEqual(text("2026-10-04"), ("продление 04.10", False))

    def test_unknown_does_not_infer_from_creation_or_limit_reset(self):
        self.assertEqual(watch.subscription_text({"createdAt": "2026-09-10", "resetsAt": "2026-10-10"}),
                         ("нет даты подписки", False))


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
        self.assertIn("codex1", text)

    def test_countdown(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(watch.countdown((now + timedelta(days=1, hours=10, minutes=5)).isoformat(), now), "1d 10h")
        self.assertEqual(watch.countdown((now + timedelta(hours=3, minutes=12)).isoformat(), now), "3h 12m")
        self.assertEqual(watch.countdown((now - timedelta(minutes=1)).isoformat(), now), "сейчас")
        self.assertEqual(watch.countdown(None, now), "")

    def test_alert_names_when_the_limit_runs_out(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        soon = (now + timedelta(hours=1, minutes=18)).isoformat()
        self.assertIn("кончится через ~1h 18m", watch.pace_alert({"pct": 99, "runs_out_at": soon}, now))
        self.assertIn("исчерпан", watch.pace_alert({"pct": 100, "runs_out_at": soon}, now))
        self.assertIn("быстрее нормы", watch.pace_alert({"pct": 40, "ahead": True, "runs_out_at": None}, now))
        self.assertEqual(watch.pace_alert({"pct": 40, "runs_out_at": None}, now), "")

    def test_claude_projection_only_when_it_runs_out_before_reset(self):
        week = {"pct": 99, "resetsAt": "2026-09-29T14:00:00Z", "willLastToReset": False,
                "projectedExhaustionAt": "2026-09-28T04:40:32Z"}
        scoped = {"pct": 35, "name": "Fable", "willLastToReset": True,
                  "projectedExhaustionAt": "2026-10-08T10:56:23Z"}
        payload = {"accounts": [{"email": "a@example.com", "usage": {"sevenDay": week, "scoped": [scoped]}}]}
        with patch.object(watch, "claude_plans", return_value={}), patch.object(watch, "run", return_value=json.dumps(payload)):
            windows = watch.fetch_claude()[0]["windows"]
        self.assertEqual([w["runs_out_at"] for w in windows], ["2026-09-28T04:40:32Z", None])

    def test_codex_projection_is_anchored_to_the_fetch(self):
        entry = usage("a@example.com", 40)
        entry["pace"] = {"secondary": {"stage": "farAhead", "willLastToReset": False, "etaSeconds": 3600}}
        with patch.object(watch, "discover_codex", return_value=([], [])), patch.object(watch, "run", return_value=json.dumps([entry])):
            window = watch.fetch_codex()[0]["windows"][0]
        left = (watch.parse_date(window["runs_out_at"]) - datetime.now(timezone.utc)).total_seconds()
        self.assertTrue(3500 < left <= 3600)

    def test_alerts_stay_whole_and_only_claude_rows_have_slots(self):
        now = datetime.now(timezone.utc)
        window = {"pct": 82, "label": "7d", "resets_at": (now + timedelta(days=2, hours=22)).isoformat(),
                  "runs_out_at": (now + timedelta(hours=21, minutes=30)).isoformat()}
        claude = [dict(provider="claude", email="alice@example.com", slot=3, plan="Max 20x", active=True, windows=[window])]
        codex = [dict(provider="codex", email="bob@example.com", plan="pro", active=False, windows=[window])]
        for columns in (100, 80):
            with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((columns, 40))):
                lines = watch.ANSI.sub("", watch.render(claude, codex, 0, 0, [])).splitlines()
            self.assertTrue(all(len(line) < columns for line in lines))
            self.assertEqual(sum("кончится через ~21h" in line for line in lines), 1)
            self.assertTrue(next(l for l in lines if "alice" in l).lstrip().startswith("3  alice"))
            self.assertTrue(next(l for l in lines if "bob" in l).lstrip().startswith("bob"))

    def test_forecast_only_for_the_active_claude_account(self):
        now = datetime.now(timezone.utc)
        window = {"pct": 82, "label": "7d", "resets_at": (now + timedelta(days=2)).isoformat(),
                  "runs_out_at": (now + timedelta(hours=5)).isoformat(), "ahead": True}
        spent = {"pct": 100, "label": "5h", "resets_at": (now + timedelta(hours=2)).isoformat()}
        claude = [dict(provider="claude", email=f"{name}@example.com", slot=i, active=i == 1, windows=[window, spent])
                  for i, name in ((1, "alice"), (2, "carol"))]
        codex = [dict(provider="codex", email="bob@example.com", active=False, windows=[window])]
        with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((120, 40))):
            lines = watch.ANSI.sub("", watch.render(claude, codex, 0, 0, [])).splitlines()
        forecasts = [line for line in lines if "кончится через" in line or "быстрее нормы" in line]
        self.assertEqual(len(forecasts), 1)
        self.assertIn("alice", forecasts[0])
        self.assertEqual(sum("исчерпан" in line for line in lines), 2)

    def test_tab_title_sums_up_the_active_account(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        calm = {"pct": 36, "label": "7d"}
        busy = {"pct": 82, "label": "7d", "runs_out_at": (now + timedelta(hours=5)).isoformat()}
        def account(active, *windows):
            return dict(provider="claude", email="alice@example.com", active=active, windows=list(windows))
        self.assertEqual(watch.tab_title([account(True, {"pct": 9, "label": "Fable"}, calm)], now), "limits · alice 36%")
        self.assertEqual(watch.tab_title([account(True, calm, busy)], now), "limits · alice 82% ▲")
        self.assertEqual(watch.tab_title([account(False, busy)], now), "limits")
        self.assertEqual(watch.tab_title(None, now), "limits")

    def test_account_column_stacks_plan_and_date_under_the_name(self):
        acc = dict(provider="codex", email="bob@example.com", plan="pro", active=False, windows=[],
                   subscription={"until": "2026-10-02T20:04:00Z"})
        with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((100, 40))):
            text = watch.render([], [acc], 0, 0, [], now=datetime(2026, 9, 28, tzinfo=timezone.utc))
        lines = [line.strip() for line in watch.ANSI.sub("", text).splitlines()]
        row = next(i for i, line in enumerate(lines) if line.startswith("bob"))
        self.assertEqual(lines[row + 1], "Pro")
        self.assertEqual(lines[row + 2], "оплачено до 02.10 (4 дн.)")

    def test_email_domain_is_hidden_only_when_shared(self):
        same = [dict(email="a@gmail.com"), dict(email="b@Gmail.com")]
        self.assertEqual(watch.shared_domain(same), "gmail.com")
        self.assertEqual(watch.shared_domain(same + [dict(email="c@corp.com")]), "")
        self.assertEqual(watch.shared_domain([dict(email="codex")]), "")

    def test_normal_cache_age_is_not_reported(self):
        now = datetime.now(timezone.utc)
        def account(minutes):
            return dict(provider="claude", email="a@example.com", active=False, windows=[{"pct": 1, "label": "7d"}],
                        fetched_at=(now - timedelta(minutes=minutes)).isoformat())
        self.assertEqual(watch.notice_text(account(6), None, now), "")
        self.assertEqual(watch.notice_text(account(20), None, now), "данные 20m назад")


if __name__ == "__main__":
    unittest.main()
