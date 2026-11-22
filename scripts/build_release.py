"""Build the release: one wheel plus install.sh and install.ps1 with the wheel's SHA-256 written in.

    python scripts/build_release.py            # writes dist/
The installers verify the wheel against the hash baked in here, so they only ever install this build.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "scripts" / "installer"


def main() -> int:
    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "middle_agentic" / "__init__.py").read_text()).group(1)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    # Run from outside the repo: a local build/ directory would shadow the `build` module.
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(ROOT)], check=True,
                   cwd=tempfile.gettempdir())
    wheel = next(dist.glob(f"middle_agentic-{version}-*.whl"))
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    for template, name in (("install.sh.tmpl", "install.sh"), ("install.ps1.tmpl", "install.ps1")):
        text = (TEMPLATES / template).read_text(encoding="utf-8")
        text = text.replace("@VERSION@", version).replace("@WHEEL@", wheel.name).replace("@SHA256@", digest)
        target = dist / name
        target.write_text(text, encoding="utf-8")
        if name.endswith(".sh"):
            target.chmod(0o755)
    print(f"version : {version}")
    print(f"wheel   : {wheel.name}")
    print(f"sha256  : {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
