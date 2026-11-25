@echo off
rem Starts DynamicsWar. Use this if double-clicking DynamicsWar.pyw does nothing.
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 DynamicsWar.pyw %* & exit /b)
where pythonw >nul 2>nul && (start "" pythonw DynamicsWar.pyw %* & exit /b)
python DynamicsWar.pyw %*
if errorlevel 1 pause
