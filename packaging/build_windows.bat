@echo off
rem Build a Windows executable of PNScope with PyInstaller.
rem Needs Python 3.10 or newer (python.org installer or "winget install Python.Python.3.12").
rem Result: dist\PNScope\PNScope.exe  and  dist\PNScope-<version>-windows.zip
setlocal
cd /d "%~dp0\.."

rem prefer "python" on PATH (the Store alias stub fails --version), else the py launcher
set PY=py -3
python --version >nul 2>nul && set PY=python

if not exist build-venv (
    %PY% -m venv build-venv || goto :error
)
call build-venv\Scripts\activate.bat || goto :error

python -m pip install --upgrade pip || goto :error
python -m pip install .[pluto] pyinstaller || goto :error

pyinstaller --noconfirm --clean packaging\pnscope.spec || goto :error

for /f %%v in ('python -c "import pnscope; print(pnscope.__version__)"') do set VERSION=%%v
powershell -NoProfile -Command "Compress-Archive -Force -Path dist\PNScope -DestinationPath dist\PNScope-%VERSION%-windows.zip" || goto :error

echo.
echo Done: dist\PNScope\PNScope.exe
echo       dist\PNScope-%VERSION%-windows.zip
exit /b 0

:error
echo.
echo Build FAILED
exit /b 1
