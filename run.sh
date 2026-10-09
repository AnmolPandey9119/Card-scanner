#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d venv ]; then
  echo "Pehli baar setup ho raha hai, thoda time lagega..."
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
else
  source venv/bin/activate
fi
if [ -z "$GROQ_API_KEY" ] && [ -f api_key.txt ]; then
  export GROQ_API_KEY="$(cat api_key.txt)"
fi
streamlit run app.py