import plistlib
import shlex
import shutil
from pathlib import Path


LAUNCH_AGENT_LABEL = "io.github.reading-manager.daily"
LAUNCH_AGENT_APP_NAME = "ReadingManager"


def default_launchagent_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def default_launchagent_log_path(home: Path) -> Path:
    return home / "Library" / "Logs" / LAUNCH_AGENT_APP_NAME / "sync-daily.log"


def default_launchagent_runtime_path(home: Path) -> Path:
    return home / "Library" / "Application Support" / LAUNCH_AGENT_APP_NAME / "app"


def default_launchagent_path_prefix(home: Path) -> str:
    paths = [
        home / ".local" / "bin",
        home / ".bun" / "bin",
        *sorted((home / ".nvm" / "versions" / "node").glob("*/bin"), reverse=True),
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
    ]
    return ":".join(str(path) for path in paths)


def sync_runtime_project(project_root: Path, runtime_root: Path) -> None:
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        project_root,
        runtime_root,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            "__pycache__",
            "*.pyc",
            ".DS_Store",
            "tmp",
        ),
    )


def build_launchagent_plist(
    project_root: Path,
    hour: int = 23,
    minute: int = 30,
    log_path: Path = None,
    runtime_root: Path = None,
) -> bytes:
    log_path = log_path or default_launchagent_log_path(Path.home())
    runtime_root = runtime_root or default_launchagent_runtime_path(Path.home())
    path_prefix = shlex.quote(default_launchagent_path_prefix(Path.home()))
    command = (
        f"mkdir -p {shlex.quote(str(log_path.parent))} && "
        f"cd {shlex.quote(str(runtime_root))} && "
        f"export PATH={path_prefix} && "
        "{ job_status=0; "
        "PYTHONPATH=src python3 -m reading_manager sync-finished-books || job_status=$?; "
        "PYTHONPATH=src python3 -m reading_manager sync-catchup || job_status=$?; "
        "PYTHONPATH=src python3 -m reading_manager sync-covers || job_status=$?; "
        "exit $job_status; } "
        f">> {shlex.quote(str(log_path))} 2>&1"
    )
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", command],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": {"LARK_IDENTITY": "user"},
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "RunAtLoad": True,
    }
    return plistlib.dumps(payload, sort_keys=True)
