#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Создаю виртуальное окружение..."
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt
python bot.py &
python checklists/app.py
