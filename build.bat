@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt pyinstaller || exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --noconsole --icon assets\icon.ico --name StockSim ^
  --paths . --add-data "web;web" --collect-all yfinance --collect-submodules uvicorn ^
  app\__main__.py || exit /b 1
echo Done: dist\StockSim.exe
