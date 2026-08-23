import shutil
import subprocess
import sys
from typing import List, Optional


class KeychainError(RuntimeError):
    pass


def is_keychain_available() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def redact_secret(value: str) -> str:
    if len(value) <= 8:
        return "*****"
    return f"{value[:4]}...{value[-4:]}"


def build_keychain_get_command(service: str, account: str) -> List[str]:
    return [
        "security",
        "find-generic-password",
        "-s",
        service,
        "-a",
        account,
        "-w",
    ]


def build_keychain_set_command(service: str, account: str, password: str) -> List[str]:
    return [
        "security",
        "add-generic-password",
        "-U",
        "-s",
        service,
        "-a",
        account,
        "-w",
        password,
    ]


def load_secret_from_keychain(service: str, account: str) -> Optional[str]:
    if not is_keychain_available():
        return None
    result = subprocess.run(
        build_keychain_get_command(service, account),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.rstrip("\n")
    if "could not be found" in result.stderr.lower() or result.returncode == 44:
        return None
    raise KeychainError(f"Failed to read macOS Keychain item for service {service}")


def save_secret_to_keychain(service: str, account: str, password: str) -> None:
    if not is_keychain_available():
        raise KeychainError("macOS Keychain is unavailable")
    result = subprocess.run(
        build_keychain_set_command(service, account, password),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise KeychainError(f"Failed to write macOS Keychain item for service {service}")
