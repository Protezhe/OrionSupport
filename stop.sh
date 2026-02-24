#!/usr/bin/env bash

stopped_any=false

if pkill -f "python bot.py" >/dev/null 2>&1; then
  echo "Бот остановлен."
  stopped_any=true
else
  echo "Бот не запущен."
fi

if pkill -f "python checklists/app.py" >/dev/null 2>&1; then
  echo "Сервер чеклистов остановлен."
  stopped_any=true
else
  echo "Сервер чеклистов не запущен."
fi

if [ "$stopped_any" = false ]; then
  exit 1
fi
