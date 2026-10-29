@REM Copyright 2026 AshleySama
@REM SPDX-License-Identifier: Apache-2.0
@echo off
setlocal
cd /d "%~dp0"
set "LOCALAPPDATA=%~dp0beta-profile"
"%~dp0PocketMemory.exe" --data-dir "%~dp0data-beta" --quit
endlocal
