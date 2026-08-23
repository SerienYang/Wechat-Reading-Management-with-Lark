import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "deploy" / "systemd" / "reading-manager-daily.service"
TIMER_PATH = ROOT / "deploy" / "systemd" / "reading-manager-daily.timer"
ENV_EXAMPLE_PATH = ROOT / "deploy" / "reading-manager.env.example"
INSTALLER_PATH = ROOT / "deploy" / "install-vps-release.sh"
SECRETS_PATH = ROOT / "deploy" / "configure-vps-secrets.sh"


class DeploymentTests(unittest.TestCase):
    def test_systemd_service_uses_hardened_runtime(self):
        service = SERVICE_PATH.read_text(encoding="utf-8")

        self.assertIn("Type=oneshot", service)
        self.assertIn("User=reading-manager", service)
        self.assertIn("Group=reading-manager", service)
        self.assertIn("Environment=HOME=/var/lib/reading-manager", service)
        self.assertIn(
            "EnvironmentFile=/etc/reading-manager/reading-manager.env",
            service,
        )
        self.assertIn(
            "/opt/reading-manager/tools/node_modules/.bin",
            service,
        )
        self.assertIn(
            "ExecStart=/opt/reading-manager/current/.venv/bin/reading-manager run-daily",
            service,
        )
        self.assertIn("TimeoutStartSec=30min", service)
        self.assertIn("PrivateTmp=true", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("ReadWritePaths=/var/lib/reading-manager", service)

    def test_systemd_timer_is_persistent_and_enableable(self):
        timer = TIMER_PATH.read_text(encoding="utf-8")

        self.assertIn("OnCalendar=*-*-* 23:30:00 Asia/Shanghai", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=reading-manager-daily.service", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_environment_example_contains_no_secret_values(self):
        values = _environment_values(ENV_EXAMPLE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(values["WEREAD_API_KEY"], "")
        self.assertEqual(values["LLM_API_KEY"], "")
        self.assertEqual(values["LLM_BASE_URL"], "https://api.deepseek.com")
        self.assertEqual(values["LLM_MODEL"], "deepseek-v4-flash")
        self.assertEqual(values["LARK_IDENTITY"], "user")
        self.assertEqual(values["READING_MANAGER_LARK_NOTIFY_USER_ID"], "")
        self.assertEqual(values["READING_MANAGER_LARK_NOTIFY_CHAT_ID"], "")
        for variable in [
            "LARK_BASE_TOKEN",
            "LARK_BASE_URL",
            "LARK_TABLE_BOOKS",
            "LARK_TABLE_READING_RECORDS",
            "LARK_TABLE_NOTES",
            "LARK_TABLE_DAILY_STATS",
            "LARK_TABLE_TAGS",
            "LARK_TABLE_SYNC_RUNS",
        ]:
            with self.subTest(variable=variable):
                self.assertIn(variable, values)
                self.assertEqual(values[variable], "")

    def test_installer_builds_versioned_release_without_enabling_timer(self):
        installer = INSTALLER_PATH.read_text(encoding="utf-8")

        self.assertIn("@larksuite/cli@1.0.44", installer)
        self.assertIn("python3-venv", installer)
        self.assertIn("python3 -m venv", installer)
        self.assertIn('APP_ROOT="/opt/reading-manager"', installer)
        self.assertIn('RELEASES_DIR="${APP_ROOT}/releases"', installer)
        self.assertIn("reading-manager:reading-manager", installer)
        self.assertIn("chmod 0700", installer)
        self.assertIn("pip install", installer)
        self.assertIn("PIP_CONFIG_FILE=/dev/null", installer)
        self.assertIn("PIP_INDEX_URL=https://pypi.org/simple", installer)
        self.assertIn('python3 -m venv "${release_dir}/.venv"', installer)
        self.assertNotIn('"${staging_dir}/.venv/bin/reading-manager"', installer)
        self.assertIn('chmod 0755 "${release_dir}"', installer)
        self.assertIn(
            'runuser -u reading-manager -- env HOME="${STATE_DIR}"',
            installer,
        )
        self.assertIn('"${release_dir}/.venv/bin/reading-manager" doctor', installer)
        self.assertIn("unittest discover", installer)
        self.assertIn("mv -Tf", installer)
        self.assertIn("systemctl daemon-reload", installer)
        self.assertNotIn("systemctl enable", installer)

    def test_secret_script_uses_hidden_input_and_restrictive_permissions(self):
        script = SECRETS_PATH.read_text(encoding="utf-8")

        self.assertIn("read -r -s", script)
        self.assertIn("umask 077", script)
        self.assertIn("WEREAD_API_KEY", script)
        self.assertIn("LLM_API_KEY", script)
        for variable in [
            "LARK_BASE_TOKEN",
            "LARK_BASE_URL",
            "LARK_TABLE_BOOKS",
            "LARK_TABLE_READING_RECORDS",
            "LARK_TABLE_NOTES",
            "LARK_TABLE_DAILY_STATS",
            "LARK_TABLE_TAGS",
            "LARK_TABLE_SYNC_RUNS",
        ]:
            with self.subTest(variable=variable):
                self.assertIn(variable, script)
        self.assertIn("chmod 0600", script)
        self.assertNotIn("set -x", script)


def _environment_values(content: str):
    values = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name] = value
    return values


if __name__ == "__main__":
    unittest.main()
