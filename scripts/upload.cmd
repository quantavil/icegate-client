@echo off
rem Windows launcher for the one-folder workflow: upload.cmd FOLDER [options]
rem Same script as scripts/icegate-run.py; uv installs its declared dependencies.
if "%~1"=="" (
  echo Usage: upload.cmd FOLDER [--dry-run] [--yes] [--no-sign] [--wait SECONDS]
  pause
  exit /b 2
)
uv run "%~dp0icegate-run.py" %*
