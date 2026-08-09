#!/usr/bin/env bash
# Обновление бота из GitHub. При любой ошибке — откат на прежнюю версию.
set -u
cd "$(dirname "$0")" || exit 1

PY=./venv/bin/python
PIP=./venv/bin/pip

if [ ! -d .git ]; then
  echo "Папка не подключена к git. Сделай разовую настройку из ИНСТРУКЦИЯ.md (раздел «Обновление одной командой»)."
  exit 1
fi

OLD=$(git rev-parse HEAD 2>/dev/null) || { echo "git не смог прочитать текущую версию"; exit 1; }

echo "Текущая версия: $(git rev-parse --short HEAD)"

if ! git fetch --prune origin 2>&1; then
  echo "Не удалось скачать обновления с GitHub (проверь доступ к репозиторию)."
  exit 1
fi

# ветка может называться main или master — определяем ту, что реально есть на GitHub
BRANCH=""
for cand in "$(git symbolic-ref --short HEAD 2>/dev/null)" main master; do
  if [ -n "$cand" ] && git rev-parse --verify "origin/$cand" >/dev/null 2>&1; then
    BRANCH="$cand"; break
  fi
done

if [ -z "$BRANCH" ]; then
  echo "Не нашёл ветку на GitHub (ожидал main или master)."
  exit 1
fi

if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ]; then
  echo "NOCHANGE Обновлений нет — на сервере уже последняя версия."
  exit 0
fi

git reset --hard "origin/$BRANCH" >/dev/null 2>&1 || { echo "Не удалось применить обновление."; exit 1; }

rollback() {
  echo "Откатываюсь на прежнюю версию…"
  git reset --hard "$OLD" >/dev/null 2>&1
  $PIP install -q -r requirements.txt >/dev/null 2>&1
}

if ! $PIP install -q -r requirements.txt 2>&1; then
  echo "Не удалось установить зависимости."
  rollback
  exit 1
fi

if ! OUT=$($PY -m py_compile bot.py store.py 2>&1); then
  echo "В новом коде синтаксическая ошибка:"
  echo "$OUT" | tail -n 5
  rollback
  exit 1
fi

if ! OUT=$(BOT_TOKEN=111111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA \
           DB_PATH=/tmp/mealbot_check.db $PY -c "import bot" 2>&1); then
  echo "Новый код не запускается:"
  echo "$OUT" | tail -n 5
  rollback
  exit 1
fi
rm -f /tmp/mealbot_check.db

echo "OK $(git rev-parse --short HEAD) $(git log -1 --pretty=%s)"
exit 0
