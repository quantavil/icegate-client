#!/usr/bin/env python3
"""Sign every unsigned .pdf/.sb/.be/.txt/.xml in a folder with the HYP2003 token.

  sign-all.py [FOLDER]        (default: the folder holding this script)

Private: this file holds the token PIN. Never print it, log it or commit it.
Needs the icegate-sign project (ICEGATE_SIGN_DIR, default ~/Documents/icegate-sign) and the
token's PKCS#11 library for this system (see that project's README).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent).expanduser().resolve()
if not target.is_dir():
    sys.exit(f"folder not found: {target}")
here = str(Path(__file__).resolve())


def windows_pkcs11() -> str | None:
    """The PKCS#11 module the HYP2003 Windows driver (vendor EXE) installs."""
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    for folder in (root / "System32", root / "SysWOW64"):
        for name in ("eps2003csp11v2.dll", "castle_v2.dll"):
            if (folder / name).is_file():
                return str(folder / name)
    return None


# icegate-sign reads this when it loads, so it must be set before the import below. On Linux the
# vendored libcastle_v2.so is found by itself.
if os.name == "nt" and not os.environ.get("ICEGATE_PKCS11_MODULE"):
    if (module := windows_pkcs11()):
        os.environ["ICEGATE_PKCS11_MODULE"] = module
    else:
        print("warning: no PKCS#11 module found; install the token's Windows driver or set ICEGATE_PKCS11_MODULE",
              file=sys.stderr)

# Linux only: started from a file manager (no terminal), reopen in one so the result is visible.
if (sys.platform.startswith("linux") and not sys.stdin.isatty() and not os.environ.get("SIGN_ALL_IN_TERMINAL")
        and (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")) and shutil.which("rio")):
    os.environ["SIGN_ALL_IN_TERMINAL"] = "1"
    os.execvp("rio", ["rio", "-e", "bash", "-c",
                      'python3 "$1" "$2"; r=$?; printf "\\nFinished (exit code %s). Press Enter to close.\\n" "$r"; read -r; exit "$r"',
                      "_", here, str(target)])

try:
    import icegate_sign  # noqa: F401
except ImportError:  # run again inside the icegate-sign environment
    project = Path(os.environ.get("ICEGATE_SIGN_DIR", "~/Documents/icegate-sign")).expanduser()
    if not project.is_dir():
        sys.exit(f"icegate-sign not installed at {project}: clone "
                 "https://github.com/quantavil/icegate-sign there, or set ICEGATE_SIGN_DIR")
    try:
        done = subprocess.run(["uv", "run", "--project", str(project), "python", here, str(target)],
                              env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}, check=False)
    except OSError as exc:
        sys.exit(f"cannot run uv ({exc.strerror}); install uv or run this inside the icegate-sign environment")
    sys.exit(done.returncode)
sys.argv = [here, str(target)]

import subprocess
import sys
import tempfile
import os
from pathlib import Path
from icegate_sign.cli import output_path

folder = Path(sys.argv[1])
allowed = {'.pdf', '.be', '.sb', '.txt', '.xml'}
files = []
claimed = {}
conflicts = False
for path in sorted(folder.iterdir()):
    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in allowed or '[signed]' in path.name.lower() or path.stem.lower().endswith('_signed'):
        continue
    dest = output_path(path)
    if dest in claimed:
        print(f'Cannot sign {path.name}: it has the same output name as {claimed[dest]}. Rename one file.', file=sys.stderr, flush=True)
        conflicts = True
        continue
    claimed[dest] = path.name
    files.append(path)
if files:
    print(f'Signing {len(files)} file(s)...', flush=True)
    with tempfile.TemporaryDirectory(prefix='.sign-all-', dir=folder) as temp_dir:
        result = subprocess.run([sys.executable, '-m', 'icegate_sign.cli', 'sign', '--pin-stdin', '--output-dir', temp_dir, '--', *(str(p) for p in files)], input='12345678\n', text=True)
        for path in files:
            staged = output_path(path, Path(temp_dir))
            if staged.is_file() and not staged.is_symlink():
                try:
                    os.replace(staged, output_path(path))
                    print(f'Saved {output_path(path).name}', flush=True)
                except OSError as e:
                    print(f'Cannot save {output_path(path).name}: {e}', file=sys.stderr, flush=True)
                    conflicts = True
    sys.exit(result.returncode or (1 if conflicts else 0))
if conflicts:
    sys.exit(1)
print('No files to sign.', flush=True)
