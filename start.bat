@echo off
setlocal

set PYTHON_EXE=C:\Users\bing\AppData\Local\Doubao\User Data\sandbox_runtime\bases\c98c5042338ed152c6f10ecd8591889f\python\python.exe
set STREAMLIT_EXE=C:\Users\bing\AppData\Local\Doubao\User Data\sandbox_runtime\bases\c98c5042338ed152c6f10ecd8591889f\python\Scripts\streamlit.exe

cd /d "%~dp0"

echo ========================================
echo   AI Toolbox - Starting Streamlit
echo ========================================
echo.

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python not found at:
    echo %PYTHON_EXE%
    echo.
    pause
    exit /b 1
)

"%PYTHON_EXE%" -m streamlit run app.py

if errorlevel 1 (
    echo.
    echo [ERROR] Streamlit exited with error.
    pause
)

endlocal
