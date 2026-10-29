@REM Copyright 2026 AshleySama
@REM SPDX-License-Identifier: Apache-2.0
@echo off
setlocal
cd /d "%~dp0"
rem Keep Beta configuration and data separate from any previous stable install.
set "LOCALAPPDATA=%~dp0beta-profile"
start "Pocket Memory Beta" "%~dp0PocketMemory.exe" --data-dir "%~dp0data-beta"
endlocal
