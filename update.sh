#!/bin/bash
set -e
cd /opt/mealplan
git pull
venv/bin/pip install -q -r requirements.txt
venv/bin/python seed.py
systemctl restart mealplan-web mealplan-bot
systemctl restart mealplan-maxbot 2>/dev/null || true
echo "Обновлено"
