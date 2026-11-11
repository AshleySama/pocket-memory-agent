rem Copyright 2026 AshleySama
rem SPDX-License-Identifier: Apache-2.0

@echo off
setlocal
cd /d "%~dp0"
set "LOCALAPPDATA=%~dp0beta-profile"
start "Pocket Memory Beta" "%~dp0PocketMemory.exe" --beta
endlocal
