@echo off
rem ----------------------------------------------------------------------------------------------
rem  Smi-Agent demo for Windows - one double-click. Fictional news, sandbox instead of real platforms.
rem  First run: creates the .venv folder and installs the project (1-2 minutes).
rem  Login: demo   Password: smi-agent-showcase   Stop: Ctrl+C
rem  Extra arguments are passed on, for example:  scripts\run-demo.cmd --port 8080
rem ----------------------------------------------------------------------------------------------
setlocal
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" goto have_venv
echo [1/3] Creating the virtual environment .venv ...
py -3 -m venv .venv 2>nul
if exist ".venv\Scripts\python.exe" goto have_venv
python -m venv .venv
if not exist ".venv\Scripts\python.exe" goto no_python

:have_venv
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto old_python

echo [2/3] Installing Smi-Agent (the first run takes 1-2 minutes) ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e .
if errorlevel 1 goto failed

echo [3/3] Starting the demo - the address is printed below. Stop with Ctrl+C.
".venv\Scripts\smi-agent.exe" serve --demo %*
goto done

:no_python
echo ERROR: Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/
echo        (tick "Add python.exe to PATH" in the installer) and run this file again.
goto done

:old_python
echo ERROR: the existing .venv folder uses Python older than 3.11. Delete the .venv folder,
echo        install Python 3.11 or newer and run this file again.
goto done

:failed
echo ERROR: installation failed - see the messages above.

:done
if not defined SMI_NO_PAUSE pause
endlocal
