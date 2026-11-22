@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo Building 1C Backup Manager v5.0 with SQL, ZIP, 7z and FTPS support...
echo.

where py >nul 2>&1
if errorlevel 1 (
    echo Python launcher "py" not found.
    echo Install Python 3.11, 3.12 or 3.13 for Windows first.
    pause
    exit /b 1
)

py -3 -m pip install --upgrade pip pyinstaller pywin32 pyodbc py7zr pystray pillow
if errorlevel 1 (
    echo Could not install dependencies.
    pause
    exit /b 2
)

py -3 -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name 1CBackupManager ^
  --hidden-import=win32timezone ^
  --hidden-import=backup_extensions ^
  --hidden-import=classic_gui ^
  --hidden-import=effector_gui ^
  --hidden-import=ui_locale ^
  --hidden-import=task_runtime ^
  --hidden-import=tray_support ^
  --hidden-import=single_instance ^
  --hidden-import=service_status ^
  --hidden-import=report_delivery ^
  --hidden-import=onec_tasks_8 ^
  --hidden-import=file_session_control ^
  --collect-all=pystray ^
  --collect-all=PIL ^
  --hidden-import=ibases_import ^
  --hidden-import=pyodbc ^
  --collect-all=py7zr ^
  onec_backup_manager.py

if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 3
)

echo.
echo Build complete: %CD%\dist\1CBackupManager.exe
echo.
pause
