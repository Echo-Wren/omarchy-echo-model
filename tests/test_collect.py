import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import collect


OPENROUTER_URL = "https://openrouter.ai/api/v1"
OTHER_URL = "https://api.example.test/v1"


def create_db(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE session_model_usage (
            session_id TEXT,
            model TEXT,
            billing_provider TEXT,
            billing_base_url TEXT,
            task TEXT,
            api_call_count INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            estimated_cost_usd REAL,
            first_seen REAL
        );
        CREATE TABLE sessions (
            id TEXT,
            title TEXT,
            model TEXT,
            started_at REAL,
            estimated_cost_usd REAL,
            billing_provider TEXT,
            billing_base_url TEXT
        );
        """
    )
    con.commit()
    con.close()


def add_usage(
    path,
    *,
    session_id,
    model="vendor/model-a",
    base_url=OPENROUTER_URL,
    provider="openrouter",
    task="",
    calls=1,
    input_tokens=0,
    output_tokens=0,
    cache_tokens=0,
    cost=0.0,
    first_seen=None,
):
    if first_seen is None:
        first_seen = time.time()
    con = sqlite3.connect(path)
    con.execute(
        """INSERT INTO session_model_usage
           (session_id, model, billing_provider, billing_base_url, task,
            api_call_count, input_tokens, output_tokens, cache_read_tokens,
            estimated_cost_usd, first_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            model,
            provider,
            base_url,
            task,
            calls,
            input_tokens,
            output_tokens,
            cache_tokens,
            cost,
            first_seen,
        ),
    )
    con.commit()
    con.close()


def add_session(
    path,
    *,
    session_id,
    started_at,
    title=None,
    model="vendor/model-a",
    cost=0.0,
    provider="openrouter",
    base_url=OPENROUTER_URL,
):
    con = sqlite3.connect(path)
    con.execute(
        """INSERT INTO sessions
           (id, title, model, started_at, estimated_cost_usd,
            billing_provider, billing_base_url)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, title, model, started_at, cost, provider, base_url),
    )
    con.commit()
    con.close()


def profile(name, home):
    home = Path(home)
    return {"name": name, "home": str(home), "db": str(home / "state.db")}


class ProfileDiscoveryTests(unittest.TestCase):
    def test_resolves_named_profile_to_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hermes"
            named = root / "profiles" / "alpha"
            named.mkdir(parents=True)
            self.assertEqual(collect.resolve_hermes_root(str(root)), str(root))
            self.assertEqual(collect.resolve_hermes_root(str(named)), str(root))

    def test_discovers_default_and_sorted_named_databases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hermes"
            create_db(root / "state.db")
            create_db(root / "profiles" / "beta" / "state.db")
            create_db(root / "profiles" / "alpha" / "state.db")
            (root / "profiles" / "no-db").mkdir()

            rows = collect.discover_profile_databases(str(root))

            self.assertEqual([row["name"] for row in rows], ["default", "alpha", "beta"])

    def test_named_database_survives_missing_default_database(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hermes"
            create_db(root / "profiles" / "alpha" / "state.db")
            rows = collect.discover_profile_databases(str(root))
            self.assertEqual([row["name"] for row in rows], ["alpha"])

    def test_duplicate_real_database_is_counted_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hermes"
            create_db(root / "state.db")
            alias = root / "profiles" / "alias"
            alias.parent.mkdir(parents=True)
            try:
                alias.symlink_to(root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks unavailable")
            rows = collect.discover_profile_databases(str(root))
            self.assertEqual([row["name"] for row in rows], ["default"])

    def test_discovers_profile_homes_even_without_databases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hermes"
            (root / "profiles" / "beta").mkdir(parents=True)
            (root / "profiles" / "alpha").mkdir()
            rows = collect.discover_profile_homes(str(root))
            self.assertEqual([row["name"] for row in rows], ["default", "alpha", "beta"])


class DatabaseFixtureCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "hermes"
        self.default = profile("default", self.root)
        self.alpha = profile("alpha", self.root / "profiles" / "alpha")
        self.beta = profile("beta", self.root / "profiles" / "beta")
        self.profile_dbs = [self.default, self.alpha, self.beta]
        for row in self.profile_dbs:
            create_db(row["db"])

        now = time.time()
        add_usage(
            self.default["db"],
            session_id="shared",
            input_tokens=10,
            output_tokens=2,
            cache_tokens=3,
            calls=2,
            cost=1.2,
            first_seen=now - 60,
        )
        add_usage(
            self.alpha["db"],
            session_id="shared",
            input_tokens=20,
            output_tokens=4,
            cache_tokens=6,
            calls=3,
            task="delegated",
            cost=2.3,
            first_seen=now - 120,
        )
        add_usage(
            self.beta["db"],
            session_id="beta-openrouter",
            model="vendor/model-b",
            input_tokens=5,
            calls=1,
            cost=0.5,
            first_seen=now - 2 * 86400,
        )
        add_usage(
            self.beta["db"],
            session_id="not-openrouter",
            model="vendor/not-openrouter",
            base_url=OTHER_URL,
            provider="other",
            input_tokens=999,
            output_tokens=999,
            calls=99,
            cost=99.0,
            first_seen=now - 60,
        )

    def tearDown(self):
        self.tempdir.cleanup()


class UsageAggregationTests(DatabaseFixtureCase):
    def test_usage_summary_aggregates_profiles_and_filters_provider(self):
        result = collect.usage_summary(self.profile_dbs)
        self.assertEqual(result["today"]["tokens"], 45)
        self.assertEqual(result["today"]["calls"], 2)
        self.assertAlmostEqual(result["today"]["cost"], 3.5, places=4)
        self.assertEqual(result["week"]["tokens"], 50)
        self.assertEqual(result["allTime"]["tokens"], 50)
        self.assertEqual(result["allTime"]["calls"], 3)
        self.assertAlmostEqual(result["allTime"]["cost"], 4.0, places=4)

    def test_by_day_merges_matching_dates_and_emits_seven_days(self):
        rows = collect.by_day_rows(self.profile_dbs)
        self.assertEqual(len(rows), 7)
        today = time.strftime("%Y-%m-%d", time.localtime())
        row = next(item for item in rows if item["date"] == today)
        self.assertEqual(row["tokens"], 45)
        self.assertAlmostEqual(row["cost"], 3.5, places=4)

    def test_by_model_merges_same_model_across_profiles(self):
        rows = collect.by_model_rows(self.profile_dbs, 30)
        self.assertEqual([row["model"] for row in rows], ["vendor/model-a", "vendor/model-b"])
        merged = rows[0]
        self.assertEqual(merged["tokens"], 45)
        self.assertEqual(merged["input"], 30)
        self.assertEqual(merged["output"], 6)
        self.assertEqual(merged["cache"], 9)
        self.assertAlmostEqual(merged["cost"], 3.5, places=4)

    def test_malformed_profile_database_does_not_block_valid_profiles(self):
        broken_path = self.root / "profiles" / "broken" / "state.db"
        broken_path.parent.mkdir(parents=True)
        broken_path.write_bytes(b"not a sqlite database")
        rows = self.profile_dbs + [profile("broken", broken_path.parent)]

        result = collect.usage_summary(rows)

        self.assertEqual(result["allTime"]["tokens"], 50)
        self.assertAlmostEqual(result["allTime"]["cost"], 4.0, places=4)


class RecentSessionsTests(DatabaseFixtureCase):
    def test_recent_sessions_are_globally_sorted_and_attributed(self):
        now = time.time()
        add_session(self.default["db"], session_id="default-new", started_at=now - 20, cost=1.0)
        add_session(self.alpha["db"], session_id="alpha-newest", started_at=now - 10, cost=2.0)
        add_session(self.beta["db"], session_id="beta-old", started_at=now - 30, cost=3.0)
        add_session(
            self.beta["db"],
            session_id="other-provider",
            started_at=now,
            provider="other",
            base_url=OTHER_URL,
        )

        rows = collect.recent_sessions(self.profile_dbs, limit=2)

        self.assertEqual([row["id"] for row in rows], ["alpha-newest", "default-new"])
        self.assertEqual([row["profile"] for row in rows], ["alpha", "default"])
        self.assertNotIn("started_at", rows[0])


class AgentRecordTests(DatabaseFixtureCase):
    def test_agent_record_aggregates_profile_activity(self):
        summary = collect.usage_summary(self.profile_dbs)
        days = collect.by_day_rows(self.profile_dbs)
        models = collect.by_model_rows(self.profile_dbs, 30)

        record = collect.agent_record(
            self.profile_dbs,
            models,
            days,
            summary["today"],
            summary["allTime"],
            account=(20.0, 5.0, 15.0),
            key_present=True,
        )

        self.assertEqual(record["todaySessions"], 2)
        self.assertEqual(record["totalSessions"], 3)
        self.assertEqual(record["todayTokensByModel"]["vendor/model-a"], 45)
        self.assertGreaterEqual(record["activeDays"], 2)
        self.assertEqual(len(record["activeDates"]), len(set(record["activeDates"])))


class OpenRouterKeyTests(unittest.TestCase):
    def test_discovers_keys_in_priority_order_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hermes"
            homes = [
                profile("default", root),
                profile("alpha", root / "profiles" / "alpha"),
                profile("beta", root / "profiles" / "beta"),
            ]
            for row in homes:
                Path(row["home"]).mkdir(parents=True, exist_ok=True)
            (root / ".env").write_text("OPENROUTER_API_KEY=root-key\n")
            (root / "profiles" / "alpha" / ".env").write_text(
                "OPENROUTER_API_KEY='shared-key'\n"
            )
            (root / "profiles" / "beta" / ".env").write_text(
                "OPENROUTER_API_KEY=shared-key\n"
            )

            keys = collect.openrouter_keys(
                homes, environ={"OPENROUTER_API_KEY": "environment-key"}
            )

            self.assertEqual(keys, ["environment-key", "root-key", "shared-key"])

    def test_ignores_blank_and_missing_env_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hermes"
            homes = [profile("default", root), profile("alpha", root / "profiles" / "alpha")]
            Path(homes[0]["home"]).mkdir(parents=True)
            (root / ".env").write_text("OPENROUTER_API_KEY=   \n")
            self.assertEqual(collect.openrouter_keys(homes, environ={}), [])

    def test_aggregate_key_usage_deduplicates_and_sums(self):
        responses = {
            "one": {"total": 1.0, "daily": 0.1, "weekly": 0.2, "monthly": 0.3},
            "two": {"total": 2.0, "daily": 0.2, "weekly": 0.4, "monthly": 0.6},
        }
        with mock.patch.object(collect, "fetch_key_usage", side_effect=lambda key: responses[key]) as fetch:
            result = collect.aggregate_key_usage(["one", "one", "two"])

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result, {"total": 3.0, "daily": 0.3, "weekly": 0.6, "monthly": 0.9})

    def test_partial_key_usage_is_unavailable(self):
        with mock.patch.object(
            collect,
            "fetch_key_usage",
            side_effect=[{"total": 1.0, "daily": 0.1, "weekly": 0.2, "monthly": 0.3}, None],
        ):
            self.assertIsNone(collect.aggregate_key_usage(["one", "two"]))
        self.assertIsNone(collect.aggregate_key_usage([]))


class CollectorIntegrationTests(unittest.TestCase):
    def test_main_emits_consolidated_profile_contract_without_keys(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "hermes"
            state_root = base / "state"
            default = profile("default", root)
            alpha = profile("alpha", root / "profiles" / "alpha")
            for row in (default, alpha):
                create_db(row["db"])
            add_usage(default["db"], session_id="default-session", input_tokens=10, calls=2, cost=1.0)
            add_usage(alpha["db"], session_id="alpha-session", input_tokens=20, calls=3, cost=2.0)
            add_session(default["db"], session_id="default-session", started_at=time.time() - 20)
            add_session(alpha["db"], session_id="alpha-session", started_at=time.time() - 10)
            (root / "config.yaml").write_text(
                "model:\n  default: openrouter/test-model\n  provider: openrouter\n"
            )
            secret = "integration-secret-key"
            (root / ".env").write_text(f"OPENROUTER_API_KEY={secret}\n")

            out_dir = state_root / "hermes-openrouter"
            agents_dir = state_root / "omarchy" / "agents" / "usage"
            patches = {
                "HERMES_ROOT": str(root),
                "DEFAULT_HERMES_HOME": str(root),
                "DB": str(root / "state.db"),
                "CFG": str(root / "config.yaml"),
                "ENV": str(root / ".env"),
                "STATE_ROOT": str(state_root),
                "OUT_DIR": str(out_dir),
                "OUT": str(out_dir / "stats.json"),
                "MODELS_CACHE": str(out_dir / "models.json"),
                "AGENTS_USAGE": str(agents_dir),
            }

            output = io.StringIO()
            with contextlib.ExitStack() as stack:
                for name, value in patches.items():
                    stack.enter_context(mock.patch.object(collect, name, value))
                stack.enter_context(mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False))
                stack.enter_context(mock.patch.object(collect, "fetch_account", return_value=(20.0, 5.0, 15.0)))
                stack.enter_context(
                    mock.patch.object(
                        collect,
                        "fetch_key_usage",
                        return_value={"total": 3.0, "daily": 3.0, "weekly": 3.0, "monthly": 3.0},
                    )
                )
                stack.enter_context(mock.patch.object(collect, "fetch_models", return_value={}))
                with contextlib.redirect_stdout(output):
                    collect.main()

            stats = json.loads((out_dir / "stats.json").read_text())
            agent = json.loads((agents_dir / "hermes.json").read_text())
            serialized = json.dumps(stats) + json.dumps(agent) + output.getvalue()

            self.assertEqual(stats["hermes"]["profileCount"], 2)
            self.assertEqual(stats["hermes"]["profiles"], ["default", "alpha"])
            self.assertEqual(stats["usage"]["allTime"]["tokens"], 30)
            self.assertEqual(stats["usage"]["allTime"]["cost"], 3.0)
            self.assertEqual(stats["usage"]["recentSessions"][0]["profile"], "alpha")
            self.assertEqual(stats["api"]["keyCount"], 1)
            self.assertTrue(stats["api"]["keyUsageComplete"])
            self.assertEqual(agent["totalSessions"], 2)
            self.assertIn("profiles=2", output.getvalue())
            self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()
