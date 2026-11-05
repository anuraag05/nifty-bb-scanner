@echo off
REM Double-click to install dependencies and open the scanner in your browser (Windows).
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% -m pip install -q -r requirements.txt
%PY% -m streamlit run app.py
pause
