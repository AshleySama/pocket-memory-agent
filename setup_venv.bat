@echo off
title Pocket Memory - Setup Virtual Environment
cd /d "%~dp0"

echo ============================================
echo  Pocket Memory - Setup Virtual Environment
echo ============================================
echo.

REM Check python availability
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found. Please install Python 3.10+ and add to PATH.
    pause
    exit /b 1
)

REM Create virtual environment if missing
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" --version >nul 2>&1
    if errorlevel 1 (
        echo [WARN] Existing .venv is invalid and will be rebuilt.
        rmdir /S /Q ".venv"
    ) else (
        echo [INFO] .venv already exists and is usable, skip creation.
    )
)
if not exist ".venv\Scripts\python.exe" (
    echo [STEP 1/4] Creating virtual environment .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)
echo.

REM Upgrade pip
echo [STEP 2/4] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.

REM Install all deps EXCEPT llama-cpp-python via Tsinghua mirror
echo [STEP 3/4] Installing dependencies (except llama-cpp-python) ...
".venv\Scripts\python.exe" -m pip install rapidocr==3.8.1 onnxruntime==1.26.0 "Pillow>=12.0,<13.0" tokenizers==0.23.1 "numpy>=1.26,<3.0" "pywebview>=5.0,<6.0" -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [ERROR] Core dependency installation failed.
    pause
    exit /b 1
)
echo.

REM Install llama-cpp-python separately via prebuilt wheel index (no compiler needed)
echo [STEP 4/4] Installing llama-cpp-python (prebuilt wheel) ...
".venv\Scripts\python.exe" -m pip install "llama-cpp-python>=0.3.0,<1.0" --prefer-binary --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo.
    echo [WARN] llama-cpp-python install failed from prebuilt index.
    echo [INFO] Trying: copy from global python site-packages ...
    for /f "delims=" %%G in ('python -c "import llama_cpp, os; print(os.path.dirname(llama_cpp.__file__))"') do set GLOBAL_LLAMA=%%G
    if defined GLOBAL_LLAMA (
        if exist "%GLOBAL_LLAMA%" (
            echo [INFO] Copying llama_cpp from global: %GLOBAL_LLAMA%
            xcopy /E /I /Y "%GLOBAL_LLAMA%" ".venv\Lib\site-packages\llama_cpp\" >nul
            if errorlevel 1 (
                echo [ERROR] Copy failed.
            ) else (
                echo [INFO] llama_cpp copied from global python.
            )
        )
    )
    REM Also copy llama_cpp.dll if present in global
    for /f "delims=" %%G in ('python -c "import llama_cpp, os; print(os.path.join(os.path.dirname(llama_cpp.__file__), 'llama_cpp', 'llama_cpp.dll'))"') do set GLOBAL_DLL=%%G
    if defined GLOBAL_DLL (
        if exist "%GLOBAL_DLL%" (
            copy /Y "%GLOBAL_DLL%" ".venv\Lib\site-packages\llama_cpp\llama_cpp\" >nul 2>&1
        )
    )
    REM Final check
    ".venv\Scripts\python.exe" -c "import llama_cpp; print('llama_cpp OK:', llama_cpp.__version__)" 2>nul
    if errorlevel 1 (
        echo.
        echo [ERROR] llama-cpp-python still not available.
        echo [INFO] Manual options:
        echo        1. Install MSVC Build Tools and rerun this script.
        echo        2. Or run the app with global python: edit run_dev.bat, set PYTHON=python
        pause
        exit /b 1
    )
)

echo.
echo ============================================
echo  Setup complete!
echo ============================================
echo  Verifying imports ...
".venv\Scripts\python.exe" -c "import numpy, onnxruntime, tokenizers, rapidocr, webview, llama_cpp; print('all imports OK')"
echo ============================================
echo  Now run run_dev.bat to start the app.
echo ============================================
pause
