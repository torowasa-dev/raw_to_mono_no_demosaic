@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

if "%~1"=="" (
  echo Drag and drop one or more Bayer RAW files onto this BAT file.
  pause
  exit /b 0
)

set "MONO_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%MONO_PYTHON%" goto dependencies

where py >nul 2>&1
if errorlevel 1 goto create_with_python
py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 goto create_with_py_default
py -3.12 -m venv "%~dp0.venv"
if errorlevel 1 goto setup_failed
goto dependencies

:create_with_py_default
py -3 -m venv "%~dp0.venv"
if errorlevel 1 goto setup_failed
goto dependencies

:create_with_python
python -m venv "%~dp0.venv"
if errorlevel 1 goto setup_failed

:dependencies
"%MONO_PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto setup_failed

set "MONO_FAILURES=0"
:convert_next
if "%~1"=="" goto completed
echo.
echo Converting: "%~1"
"%MONO_PYTHON%" "%~dp0raw_to_mono_no_demosaic.py" "%~f1" --format both
if errorlevel 1 set /a MONO_FAILURES+=1 >nul
shift
goto convert_next

:completed
echo.
echo Finished. Failed files: %MONO_FAILURES%
pause
if not "%MONO_FAILURES%"=="0" exit /b 1
exit /b 0

:setup_failed
echo.
echo Setup failed. Install Python 3.12 and check your internet connection.
pause
exit /b 1
