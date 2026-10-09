#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d venv ]; then
  echo "Pehli baar setup ho raha hai, thoda time lagega..."
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
  pip uninstall -y opencv-python
  pip install opencv-python-headless
else
  source venv/bin/activate
fi
streamlit run app.py