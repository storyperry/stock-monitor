@echo off
rem Stock Monitor - silent start (no console window)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py"
