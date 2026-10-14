@echo off
chcp 65001 > nul
title Birge Telegram Bot (@BirgeAuthBot)
echo =======================================================
echo          Запуск Telegram-бота сервиса Birge
echo =======================================================
cd /d "%~dp0"
".\venv\Scripts\python.exe" -u run_bot.py
pause
