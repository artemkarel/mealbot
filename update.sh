#!/bin/bash
set -e
cd /opt/mealplan
# репозиторий приватный: сервер тянет код по SSH своим deploy-ключом (~/.ssh/mealbot_deploy,
# алиас github-mealbot в ~/.ssh/config). Переключаемся сами, как только ключ добавлен в GitHub.
if git remote get-url origin | grep -q '^https://' &&
   ssh -o BatchMode=yes -o ConnectTimeout=10 -T git@github-mealbot 2>&1 | grep -q 'successfully authenticated'; then
  git remote set-url origin git@github-mealbot:artemkarel/mealbot.git
  echo "Источник кода переключён на SSH (deploy-ключ)"
fi
git pull
venv/bin/pip install -q -r requirements.txt
venv/bin/python seed.py
systemctl restart mealplan-web mealplan-bot
systemctl restart mealplan-maxbot 2>/dev/null || true
echo "Обновлено"
