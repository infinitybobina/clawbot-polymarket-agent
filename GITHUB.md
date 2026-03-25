# Настройка работы с GitHub

Рабочая папка проекта: **`c:\Dev\Clawbot-polymarket-agent-clean`**

## Первоначальная настройка (один раз)

### 1. Инициализация репозитория (если ещё не сделано)

```powershell
cd c:\Dev\Clawbot-polymarket-agent-clean
git init
```

### 2. Подключение удалённого репозитория

Если репозиторий уже создан на GitHub:

```powershell
git remote add origin https://github.com/ВАШ_USERNAME/clawbot-polymarket-agent.git
```

или по SSH:

```powershell
git remote add origin git@github.com:ВАШ_USERNAME/clawbot-polymarket-agent.git
```

Замените `ВАШ_USERNAME` на ваш логин GitHub.

### 3. Первый коммит и отправка

```powershell
git add .
git status   # проверьте: .env и логи не должны попасть (см. .gitignore)
git commit -m "Initial commit: clawbot-polymarket-agent-clean"
git branch -M main
git push -u origin main
```

## Важно

- Файл **`.env`** с токенами и ключами в репозиторий **не попадает** (указан в `.gitignore`). Создайте `.env` локально в папке `c:\Dev\Clawbot-polymarket-agent-clean` по образцу (см. OBSERVABILITY.md, PRODUCTION_READY.md).
- Логи (`clawbot_run.log`, `clawbot_v2_run.log`) и файлы состояния (`sl_cooldown.json`, `tp_cooldown.json`, `portfolio_state.json`) тоже игнорируются — это локальное состояние.

## Смена рабочей папки

Если вы переехали со старой папки `c:\Dev\Clawbot\clawbot-polymarket-agent`:

1. Все пути в **SCHEDULER.md** уже приведены к `c:\Dev\Clawbot-polymarket-agent-clean`.
2. В Планировщике заданий Windows обновите задачу ClawBot: в **Действия** укажите новый путь к `main.py` и новую **Рабочую папку**: `c:\Dev\Clawbot-polymarket-agent-clean`.
3. Файл `.env` скопируйте в новую папку вручную (или создайте заново) — он не в git.
