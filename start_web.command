#!/bin/zsh
cd "$(dirname "$0")"
export MAX_CONCURRENT_JOBS=3
python3 web_app.py
