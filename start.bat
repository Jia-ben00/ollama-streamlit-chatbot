@echo off
REM ==================================================================
REM  AI Toolbox launcher (Windows)
REM
REM  This script is portable on purpose: it contains no hard-coded
REM  interpreter path. The interpreter is resolved in this order:
REM
REM    1. the PYTHON_EXE environment variable (if set and it exists)
REM    2. "py -3"  - the launcher shipped with python.org installers
REM    3. "python" - whatever comes first on PATH
REM
REM  Usage:
REM    start.bat
REM    set PYTHON_EXE=C:\Python312\python.exe && start.bat
REM ==================================================================

setlocal
cd /d "%~dp0"

echo ========================================
echo   AI Toolbox - Starting Streamlit
echo ========================================
echo.

set "PY="

REM --- 1) explicit override -----------------------------------------
if defined PYTHON_EXE (
    if exist "%PYTHON_EXE%" (
        set "PY=%PYTHON_EXE%"
    ) else (
        echo [WARN] PYTHON_EXE is set but does not exist:
        echo        %PYTHON_EXE%
        echo        Falling back to auto-detection.
        echo.
    )
)

REM --- 2) py launcher ------------------------------------------------
if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)

REM --- 3) python on PATH ---------------------------------------------
if not defined PY (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    echo [ERROR] No usable Python interpreter found.
    echo.
    echo   Install Python 3.10+ from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during setup.
    echo.
    echo   Or point this script at an interpreter you already have:
    echo       set PYTHON_EXE=C:\Python312\python.exe
    echo       start.bat
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo [INFO] Interpreter: %PY%  (Python %PYVER%)
echo.

REM --- dependency check ----------------------------------------------
%PY% -c "import streamlit" >nul 2>&1
if not errorlevel 1 goto :launch

echo [WARN] streamlit is not installed for this interpreter.
choice /c YN /n /m "Install the runtime dependencies now (pip install -r requirements.txt)? [Y/N] "
if errorlevel 2 goto :noinstall

echo.
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed. See the pip output above.
    pause
    exit /b 1
)
goto :launch

:noinstall
echo.
echo [ERROR] Cannot start without streamlit. Run:
echo         %PY% -m pip install -r requirements.txt
echo.
pause
exit /b 1

:launch
echo.
echo Starting Streamlit... (press Ctrl+C to stop)
echo.
%PY% -m streamlit run app.py

if errorlevel 1 (
    echo.
    echo [ERROR] Streamlit exited with an error. See the log above.
    pause
)

endlocal
