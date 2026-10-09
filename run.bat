@echo off
cd /d %~dp0
if not exist venv (
  echo Pehli baar setup ho raha hai, thoda time lagega...
  python -m venv venv
  call venv\Scripts\activate
  pip install -r requirements.txt
) else (
  call venv\Scripts\activate
)
if "%GEMINI_API_KEY%"=="" (
  if exist api_key.txt (
    set /p GEMINI_API_KEY=<api_key.txt
  )
)
streamlit run app.py
pause