import plistlib
import tempfile
import unittest
from pathlib import Path

from reading_manager.launchagent import (
    LAUNCH_AGENT_LABEL,
    build_launchagent_plist,
    default_launchagent_log_path,
    default_launchagent_path,
    default_launchagent_path_prefix,
    default_launchagent_runtime_path,
    sync_runtime_project,
)


class LaunchAgentTests(unittest.TestCase):
    def test_launchagent_label_is_project_generic(self):
        self.assertEqual(
            LAUNCH_AGENT_LABEL,
            "io.github.reading-manager.daily",
        )

    def test_build_launchagent_plist_runs_daily_sync_at_2330(self):
        log_path = Path("/Users/example/Library/Logs/ReadingManager/sync-daily.log")
        runtime_path = Path("/Users/example/Library/Application Support/ReadingManager/app")
        payload = plistlib.loads(
            build_launchagent_plist(
                Path("/tmp/reading"),
                hour=23,
                minute=30,
                log_path=log_path,
                runtime_root=runtime_path,
            )
        )

        self.assertEqual(payload["Label"], LAUNCH_AGENT_LABEL)
        self.assertEqual(payload["StartCalendarInterval"], {"Hour": 23, "Minute": 30})
        self.assertTrue(payload["RunAtLoad"])
        self.assertIn("sync-finished-books", payload["ProgramArguments"][-1])
        self.assertIn("sync-catchup", payload["ProgramArguments"][-1])
        self.assertIn("sync-covers", payload["ProgramArguments"][-1])
        self.assertIn("job_status=0", payload["ProgramArguments"][-1])
        self.assertNotIn(" status=0", payload["ProgramArguments"][-1])
        self.assertIn("mkdir -p /Users/example/Library/Logs/ReadingManager", payload["ProgramArguments"][-1])
        self.assertIn("cd '/Users/example/Library/Application Support/ReadingManager/app'", payload["ProgramArguments"][-1])
        self.assertNotIn("cd /tmp/reading", payload["ProgramArguments"][-1])
        self.assertIn(str(log_path), payload["ProgramArguments"][-1])
        self.assertEqual(payload["StandardOutPath"], str(log_path))
        self.assertEqual(payload["StandardErrorPath"], str(log_path))
        self.assertEqual(payload["EnvironmentVariables"]["LARK_IDENTITY"], "user")
        self.assertNotIn("LLM_API_KEY", payload["EnvironmentVariables"])
        self.assertNotIn("LLM_API_KEY", payload["ProgramArguments"][-1])

    def test_default_launchagent_path_uses_user_launchagents(self):
        self.assertEqual(
            default_launchagent_path(Path("/Users/example")),
            Path(
                "/Users/example/Library/LaunchAgents/"
                "io.github.reading-manager.daily.plist"
            ),
        )

    def test_default_launchagent_log_path_uses_user_library_logs(self):
        self.assertEqual(
            default_launchagent_log_path(Path("/Users/example")),
            Path("/Users/example/Library/Logs/ReadingManager/sync-daily.log"),
        )

    def test_default_launchagent_runtime_path_uses_application_support(self):
        self.assertEqual(
            default_launchagent_runtime_path(Path("/Users/example")),
            Path("/Users/example/Library/Application Support/ReadingManager/app"),
        )

    def test_default_launchagent_path_prefix_includes_user_tool_bins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            (home / ".nvm" / "versions" / "node" / "v24.14.1" / "bin").mkdir(parents=True)

            path_prefix = default_launchagent_path_prefix(home)

            self.assertIn(str(home / ".nvm" / "versions" / "node" / "v24.14.1" / "bin"), path_prefix)
            self.assertIn(str(home / ".local" / "bin"), path_prefix)
            self.assertNotIn("/Applications/Codex.app/Contents/Resources", path_prefix)

    def test_sync_runtime_project_copies_safe_runtime_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            runtime = root / "runtime" / "app"
            (source / "src").mkdir(parents=True)
            (source / "tests").mkdir()
            (source / ".git").mkdir()
            (source / "tmp").mkdir()
            (source / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
            (source / "tests" / "test_app.py").write_text("pass", encoding="utf-8")
            (source / ".git" / "config").write_text("secret-ish", encoding="utf-8")
            (source / "tmp" / "state.json").write_text("cookie", encoding="utf-8")
            (runtime / "stale.txt").parent.mkdir(parents=True)
            (runtime / "stale.txt").write_text("old", encoding="utf-8")

            sync_runtime_project(source, runtime)

            self.assertTrue((runtime / "src" / "app.py").exists())
            self.assertTrue((runtime / "tests" / "test_app.py").exists())
            self.assertFalse((runtime / ".git").exists())
            self.assertFalse((runtime / "tmp" / "state.json").exists())
            self.assertFalse((runtime / "stale.txt").exists())


if __name__ == "__main__":
    unittest.main()
