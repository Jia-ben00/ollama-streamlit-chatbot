@echo off
chcp 65001 >nul
echo ========================================
echo   AI 工具箱启动脚本
echo ========================================
echo.

set PYTHON_EXE=C:\Users\bing\AppData\Local\Doubao\User Data\sandbox_runtime\bases\c98c5042338ed152c6f10ecd8591889f\python\python.exe
set STREAMLIT_EXE=C:\Users\bing\AppData\Local\Doubao\User Data\sandbox_runtime\bases\c98c5042338ed152c6f10ecd8591889f\python\Scripts\streamlit.exe

cd /d "%~dp0"

echo 正在启动 Streamlit...
echo.
"%PYTHON_EXE%" -m streamlit run app.py

pause
