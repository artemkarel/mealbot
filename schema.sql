PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, shelf_days INT DEFAULT 14,
  freezable INT DEFAULT 0, category TEXT, pack REAL, unit TEXT DEFAULT 'г', url TEXT,
  kcal REAL, prot REAL, fat REAL, carb REAL,       -- КБЖУ на 100 г/мл сырого продукта
  unit_g REAL DEFAULT 1);                          -- граммов в 1 штуке (для unit='шт')

CREATE TABLE IF NOT EXISTS dishes (
  id INTEGER PRIMARY KEY, dish TEXT NOT NULL, type TEXT,
  product TEXT REFERENCES products(id), amount REAL, unit TEXT, coef REAL, note TEXT);
CREATE INDEX IF NOT EXISTS idx_dishes_name ON dishes(dish);

CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY, title TEXT, date_from TEXT, date_to TEXT,
  source_file TEXT, raw_json TEXT, created_at TEXT DEFAULT (datetime('now')),
  active INT DEFAULT 1, persons INT DEFAULT 1, user_id INT);

CREATE TABLE IF NOT EXISTS plan_items (
  id INTEGER PRIMARY KEY, plan_id INT REFERENCES plans(id) ON DELETE CASCADE,
  day_index INT, day_name TEXT, meal TEXT, meal_index INT, optional INT DEFAULT 0,
  name TEXT, qty_min REAL, qty_max REAL, unit TEXT, raw_line TEXT, note TEXT, url TEXT);
CREATE INDEX IF NOT EXISTS idx_items_plan ON plan_items(plan_id, day_index);

CREATE TABLE IF NOT EXISTS recipes (
  id INTEGER PRIMARY KEY, plan_id INT, dish TEXT, title TEXT,
  ingredients_json TEXT, steps_json TEXT);

CREATE TABLE IF NOT EXISTS plan_links (           -- ссылки на товары из примечаний плана
  id INTEGER PRIMARY KEY, plan_id INT, name TEXT, url TEXT,
  product TEXT);                                  -- товар справочника, к которому относится ссылка
CREATE INDEX IF NOT EXISTS idx_plan_links ON plan_links(plan_id);

CREATE TABLE IF NOT EXISTS prep_tasks (
  id INTEGER PRIMARY KEY, plan_id INT, day_index INT, meal TEXT, text TEXT, done INT DEFAULT 0);

CREATE TABLE IF NOT EXISTS purchases (
  id INTEGER PRIMARY KEY, product TEXT REFERENCES products(id), amount REAL, unit TEXT,
  bought_at TEXT DEFAULT (date('now')), expires_at TEXT, frozen INT DEFAULT 0, used INT DEFAULT 0,
  user_id INT);

CREATE TABLE IF NOT EXISTS meal_logs (
  id INTEGER PRIMARY KEY, plan_id INT, day_index INT, meal TEXT,
  status TEXT, comment TEXT, at TEXT DEFAULT (datetime('now')));

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS user_prefs (
  user_id INTEGER PRIMARY KEY, current_plan_id INT, persons INT DEFAULT 1,
  tz TEXT,                                                 -- пояс телефона: IANA или "+03:00"
  sex TEXT, height REAL, weight REAL,                      -- для расчётов диетолога
  health TEXT);                                            -- болезни/аллергии, видны только владельцу

CREATE TABLE IF NOT EXISTS user_reminders (
  id INTEGER PRIMARY KEY, user_id INT, kind TEXT,          -- meal | shopping | morning | evening
  meal TEXT, time TEXT, enabled INT DEFAULT 1);

CREATE TABLE IF NOT EXISTS supplements (
  id INTEGER PRIMARY KEY, user_id INT, name TEXT, dose TEXT,
  meal TEXT, timing TEXT);                                  -- до еды | во время еды | после еды

CREATE TABLE IF NOT EXISTS user_recipes (
  id INTEGER PRIMARY KEY, user_id INT, dish TEXT, title TEXT,
  ingredients_json TEXT, steps_json TEXT,
  created_at TEXT DEFAULT (datetime('now')));

CREATE TABLE IF NOT EXISTS user_places (            -- «где купить» поверх products.url
  user_id INT, product TEXT, place TEXT,
  PRIMARY KEY (user_id, product));

CREATE TABLE IF NOT EXISTS week_plans (             -- какой план выбран на какую неделю
  user_id INT, week_start TEXT, plan_id INT,        -- week_start = понедельник, YYYY-MM-DD
  PRIMARY KEY (user_id, week_start));

CREATE TABLE IF NOT EXISTS menu_share (           -- общее меню: планы владельца видны получателю
  owner_id INT, follower_id INT,
  created_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (owner_id, follower_id));

CREATE TABLE IF NOT EXISTS users (                  -- кто пользуется ботом/приложением
  user_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, username TEXT,
  first_seen TEXT DEFAULT (datetime('now')), last_seen TEXT, source TEXT);

CREATE TABLE IF NOT EXISTS meal_overrides (         -- «не хочу это» — замена приёма пищи
  user_id INT, plan_id INT, day_index INT, meal TEXT,
  items_json TEXT, created_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, plan_id, day_index, meal));
