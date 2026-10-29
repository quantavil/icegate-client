#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 - "$repo" "$@" <<'PY'
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

repo = Path(sys.argv[1])
parser = argparse.ArgumentParser(description="Sign selected files and prepare an ICEGATE plan; never uploads.")
parser.add_argument("kind", choices=("pdf", "filing"))
parser.add_argument("--type", dest="doc_type", help="document code for all PDFs")
parser.add_argument("files", nargs="+", type=Path)
args = parser.parse_args(sys.argv[2:])
if (args.kind == "pdf") != bool(args.doc_type):
    parser.error("pdf requires --type; filing does not accept --type")

files = [p.expanduser().absolute() for p in args.files]
allowed = {".pdf"} if args.kind == "pdf" else {".sb", ".be"}
outputs = [p.with_name(re.sub(r"\s+", "_", p.stem) + "_Signed" + p.suffix) for p in files]
if any(p.is_symlink() or not p.is_file() or p.suffix.lower() not in allowed or p.stem.lower().endswith("_signed") or "[signed]" in p.name.lower() for p in files):
    parser.error("inputs must be regular unsigned files of the selected kind")
if len({p.name for p in files}) != len(files) or len({p.name for p in outputs}) != len(outputs):
    parser.error("input or output names collide")
if any(os.path.lexists(p) for p in outputs):
    parser.error("a signed output already exists")

signer = Path(os.environ.get("ICEGATE_SIGN_SCRIPT", "~/Downloads/Signer/sign-all.sh")).expanduser()
if not signer.is_file():
    parser.error("private signer script not found")

try:
    with tempfile.TemporaryDirectory(prefix="icegate-sign-") as temp:
        folder = Path(temp)
        private_script = folder / "sign-all.sh"
        shutil.copyfile(signer, private_script)
        private_script.chmod(0o700)
        for source in files:
            shutil.copyfile(source, folder / source.name)
        env = {**os.environ, "SIGN_ALL_IN_TERMINAL": "1"}
        result = subprocess.run([str(private_script)], env=env, check=False)
        if result.returncode:
            sys.exit(result.returncode)
        signed = [folder / p.name for p in outputs]
        if any(p.is_symlink() or not p.is_file() for p in signed):
            raise RuntimeError("signer did not produce every expected output")
        for source, dest in zip(signed, outputs):
            with source.open("rb") as inp, dest.open("xb") as out:
                shutil.copyfileobj(inp, out)
            print(f"Saved {dest}", flush=True)
    command = ["uv", "run", "icegate"]
    if args.kind == "pdf":
        command += ["esanchit", "submit", "--dry-run", "--doc-type", args.doc_type]
    else:
        command += ["filing", "upload", "--dry-run"]
    sys.exit(subprocess.run(command + [str(p) for p in outputs], cwd=repo, check=False).returncode)
except (OSError, RuntimeError) as exc:
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(1)
PY
