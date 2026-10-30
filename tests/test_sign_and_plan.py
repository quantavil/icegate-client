from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="sign-and-plan.sh is Linux/macOS only")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sign-and-plan.sh"


def test_signs_only_requested_file_and_runs_dry_run(tmp_path):
    signer = tmp_path / "signer.sh"
    signer.write_text('#!/bin/bash\nfor f in "$(dirname "$0")"/*.sb; do cp "$f" "${f%.sb}_Signed.sb"; done\n')
    signer.chmod(0o700)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$TEST_ARGS"\n')
    uv.chmod(0o755)
    source = tmp_path / "one.sb"
    source.write_text("file")
    (tmp_path / "other.sb").write_text("other")
    env = {**os.environ, "ICEGATE_SIGN_SCRIPT": str(signer), "PATH": f'{bin_dir}:{os.environ["PATH"]}', "TEST_ARGS": str(tmp_path / "args")}
    result = subprocess.run(["bash", str(SCRIPT), "filing", "--", str(source)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "one_Signed.sb").read_text() == "file"
    assert not (tmp_path / "other_Signed.sb").exists()
    assert (tmp_path / "args").read_text().splitlines() == ["run", "icegate", "filing", "upload", "--dry-run", str(tmp_path / "one_Signed.sb")]


def test_refuses_existing_signed_output_before_signing(tmp_path):
    source = tmp_path / "one.sb"
    source.write_text("file")
    output = tmp_path / "one_Signed.sb"
    output.write_text("existing")
    result = subprocess.run(["bash", str(SCRIPT), "filing", "--", str(source)], capture_output=True, text=True)
    assert result.returncode != 0
    assert output.read_text() == "existing"
    assert "already exists" in result.stderr


def test_signer_failure_never_creates_output_or_plans(tmp_path):
    signer = tmp_path / "signer.sh"
    signer.write_text("#!/bin/bash\nexit 7\n")
    signer.chmod(0o700)
    source = tmp_path / "one.pdf"
    source.write_text("file")
    env = {**os.environ, "ICEGATE_SIGN_SCRIPT": str(signer)}
    result = subprocess.run(["/bin/bash", str(SCRIPT), "pdf", "--type", "331000", "--", str(source)], env=env, capture_output=True, text=True)
    assert result.returncode == 7, result.stderr
    assert not (tmp_path / "one_Signed.pdf").exists()
