@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw streamdeck_app.pyw) || (start "" pythonw streamdeck_app.pyw)
