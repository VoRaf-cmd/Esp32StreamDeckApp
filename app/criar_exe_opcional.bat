@echo off
cd /d "%~dp0"
py -m pip install pyinstaller
py -m PyInstaller --noconsole --onefile --name StreamDeckESP32 --collect-all customtkinter --collect-all comtypes --collect-all pycaw --hidden-import pystray._win32 streamdeck_app.pyw
echo.
echo O programa fica em dist\StreamDeckESP32.exe
pause
