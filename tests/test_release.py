"""Installers: rendered correctly, shell-valid, and role handling happens before anything is installed."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from middle_agentic import __version__

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_release  # noqa: E402

DIGEST = "ab" * 32
WHEEL = f"middle_agentic-{__version__}-py3-none-any.whl"


def rendered(name: str) -> str:
    return build_release.render((ROOT / "scripts" / "installer" / f"{name}.tmpl").read_text(encoding="utf-8"),
                                __version__, WHEEL, DIGEST)


def test_installers_have_no_placeholders_left() -> None:
    for name in ("install.sh", "install.ps1"):
        text = rendered(name)
        assert "@VERSION@" not in text and "@WHEEL@" not in text and "@SHA256@" not in text
        assert DIGEST in text and __version__ in text


def test_shell_installer_is_valid_sh(tmp_path) -> None:
    script = tmp_path / "install.sh"
    script.write_text(rendered("install.sh"), encoding="utf-8")
    result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_shell_installer_rejects_unknown_role_before_installing(tmp_path) -> None:
    if shutil.which("sh") is None:
        pytest.skip("no sh")
    script = tmp_path / "install.sh"
    script.write_text(rendered("install.sh"), encoding="utf-8")
    result = subprocess.run(["sh", str(script), "--role", "bogus"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "client veya gateway" in result.stderr


def test_powershell_installer_supports_the_gateway_role() -> None:
    text = rendered("install.ps1")
    assert "MAGENT_ROLE" in text and "gateway-setup" in text and "client-setup" in text
    assert "SHA-256 eşleşmedi" in text  # refuses a mismatching wheel


def test_shell_installer_runs_inside_main_so_piped_stdin_is_not_consumed() -> None:
    text = rendered("install.sh")
    assert "main() {" in text and text.rstrip().endswith('main "$@"')


def test_shell_installer_does_not_use_homebrew_and_protects_stdin() -> None:
    text = rendered("install.sh")
    assert "brew install" not in text
    assert "magent-pipx" in text  # pipx lives in its own venv
    assert 'gateway-setup --from-repo "$MAGENT_DATA_DIR" </dev/null' in text
    assert 'curl -fsSL -o "$TMP/$WHEEL" "$URL" </dev/null' in text
