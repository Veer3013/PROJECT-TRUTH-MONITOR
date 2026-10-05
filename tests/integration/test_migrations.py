from __future__ import annotations

import os
import subprocess
import sys


def test_alembic_upgrades_fresh_database_and_has_no_pending_models(tmp_path) -> None:
    database = tmp_path / "fresh.db"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    upgraded = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    checked = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "No new upgrade operations" in checked.stdout

