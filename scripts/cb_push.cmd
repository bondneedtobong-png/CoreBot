@echo off
setlocal enabledelayedexpansion

set "BRANCH=main"
if not "%~2"=="" set "BRANCH=%~2"
set "MSG=%~1"

if "%MSG%"=="" (
  echo Usage: cb_push.cmd "commit message" [branch]
  echo Example: cb_push.cmd "update neurochat flow" main
  exit /b 1
)

echo [1/5] Checking git repo...
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo ERROR: current directory is not a git repository.
  exit /b 1
)

echo [2/5] Staging changes...
git add -A

echo [3/5] Creating commit...
git diff --cached --quiet
if not errorlevel 1 (
  echo INFO: no staged changes, skipping commit.
) else (
  git commit -m "%MSG%"
  if errorlevel 1 (
    echo ERROR: commit failed.
    exit /b 1
  )
)

echo [4/5] Pushing to origin/%BRANCH%...
git push origin %BRANCH%
if errorlevel 1 (
  echo ERROR: push failed.
  exit /b 1
)

echo [5/5] Done. Current status:
git status -sb

endlocal

