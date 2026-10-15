# Деплой TGTranslate на сервер

После настройки каждый `git push` в ветку `main` автоматически:

1. прогоняет `ruff` и `pytest` — если тесты упали, деплоя не будет;
2. собирает Docker-образ и кладёт его в GitHub Container Registry (`ghcr.io`);
3. заходит на сервер по SSH и перезапускает контейнеры с новым образом.

База, очередь и скачанные модели хранятся в Docker-томах и переживают обновления.
Все сервисы работают с `restart: unless-stopped`: упавший процесс перезапускается,
а после перезагрузки сервера всё поднимается само.

Шаги 1–5 делаются один раз.

---

## Шаг 1. Требования к серверу

- Linux (Ubuntu 22.04/24.04 или Debian 12), доступ по SSH с правами `sudo`.
- RAM: от 8 ГБ для `qwen2.5:7b`, от 4 ГБ для `qwen2.5:3b`.
- Диск: от 20 ГБ (модели Ollama весят ~5 ГБ).
- Открывать порты не нужно: бот работает через long polling. Домен и HTTPS тоже не нужны.

## Шаг 2. Репозиторий на GitHub

1. Открой https://github.com/new
2. Имя: `tgtranslate`, видимость: **Private**. README, `.gitignore` и лицензию **не** добавляй.
3. Нажми **Create repository**.
4. На Mac:

   ```bash
   cd ~/GIT/TGTranslate
   git remote add origin git@github.com:esenin9426/tgtranslate.git
   git push -u origin main
   ```

   Первый запуск пайплайна упадёт на шаге `deploy`, потому что секретов ещё нет. Это нормально,
   его перезапустим в шаге 6.

## Шаг 3. Подготовка сервера

Зайди на сервер (`ssh root@<IP_сервера>` или своим пользователем) и выполни:

```bash
# Docker и плагин docker compose
curl -fsSL https://get.docker.com | sh

# Docker стартует при загрузке системы, а с ним и контейнеры
sudo systemctl enable --now docker

# Отдельный пользователь для деплоя с доступом к Docker
sudo useradd -m -s /bin/bash deploy
sudo usermod -aG docker deploy

# Каталог проекта
sudo -u deploy mkdir -p /home/deploy/tgtranslate
```

Проверка:

```bash
sudo -u deploy docker compose version
```

## Шаг 4. Файл `.env` на сервере

Сгенерируй два секрета (можно прямо на сервере):

```bash
openssl rand -hex 32   # для API_TOKEN
openssl rand -hex 24   # для POSTGRES_PASSWORD
```

Создай файл:

```bash
sudo -u deploy nano /home/deploy/tgtranslate/.env
```

Минимальное содержимое (остальные параметры смотри в `.env.example`):

```env
BOT_TOKEN=токен_от_@BotFather
API_TOKEN=результат_первой_команды
POSTGRES_PASSWORD=результат_второй_команды

# Telegram id через запятую, кому разрешено пользоваться ботом (пусто — всем)
ALLOWED_TG_IDS=

TRANSLATOR=ollama
# На слабом сервере (меньше 8 ГБ RAM) поставь qwen2.5:3b
OLLAMA_MODEL=qwen2.5:7b
TARGET_LANG=ru
TARGET_LANG_NAME=Russian
```

Закрой файл от чужих глаз:

```bash
sudo chmod 600 /home/deploy/tgtranslate/.env
```

> Если этот же бот уже запущен локально у тебя на Mac, останови его (`docker compose down`).
> Два экземпляра с одним `BOT_TOKEN` мешают друг другу при long polling.

## Шаг 5. SSH-ключ для GitHub Actions

На **Mac**:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/tgtranslate_deploy -N "" -C "github-actions-deploy"
ssh-copy-id -i ~/.ssh/tgtranslate_deploy.pub deploy@<IP_сервера>
```

Если у пользователя `deploy` нет пароля и `ssh-copy-id` не проходит, добавь ключ вручную на сервере:

```bash
sudo -u deploy mkdir -p /home/deploy/.ssh
echo "<содержимое ~/.ssh/tgtranslate_deploy.pub>" | sudo -u deploy tee -a /home/deploy/.ssh/authorized_keys
sudo chmod 700 /home/deploy/.ssh && sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

Проверка с Mac (должно напечатать версию Docker без запроса пароля):

```bash
ssh -i ~/.ssh/tgtranslate_deploy deploy@<IP_сервера> docker --version
```

## Шаг 6. Секреты в GitHub

Репозиторий → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Имя        | Значение                                                                  |
|------------|---------------------------------------------------------------------------|
| `SSH_HOST` | IP или домен сервера                                                      |
| `SSH_USER` | `deploy`                                                                  |
| `SSH_KEY`  | всё содержимое `~/.ssh/tgtranslate_deploy` (приватный ключ, без `.pub`)   |
| `SSH_PORT` | только если SSH не на 22 порту                                            |

Скопировать приватный ключ в буфер обмена: `pbcopy < ~/.ssh/tgtranslate_deploy`.

Необязательно: в **Variables** можно задать `DEPLOY_DIR`, каталог относительно домашнего
каталога `deploy`. По умолчанию это `tgtranslate`.

Перезапусти деплой: вкладка **Actions** → **CI/CD** → **Run workflow** → ветка `main`.

## Шаг 7. Проверка

1. В **Actions** все три задачи (`test`, `build`, `deploy`) должны стать зелёными.
2. На сервере:

   ```bash
   sudo -iu deploy
   cd ~/tgtranslate
   docker compose --env-file .env --env-file .deploy.env ps
   ```

   `postgres`, `redis`, `ollama`, `api`, `worker`, `bot` должны быть в статусе `running`/`healthy`.
   `migrate` и `ollama-pull` — одноразовые, у них статус `exited (0)`.
3. Первый запуск дольше обычного: `ollama-pull` скачивает модели (несколько гигабайт).
   За прогрессом можно следить так:
   `docker compose --env-file .env --env-file .deploy.env logs -f ollama-pull`
4. Напиши боту в Telegram `/start`.

---

## Ежедневная работа

```bash
git add . && git commit -m "что поменял"
git push            # в main → тесты → сборка → деплой
```

Ход деплоя виден во вкладке **Actions**. Pull request'ы и пуши в другие ветки только прогоняют тесты
и ничего не деплоят.

## Команды на сервере

```bash
sudo -iu deploy && cd ~/tgtranslate
alias dc='docker compose --env-file .env --env-file .deploy.env'

dc ps                          # статус сервисов
dc logs -f bot worker          # логи (Ctrl+C — выйти)
dc logs --tail 100 api         # последние 100 строк
dc restart worker              # перезапустить один сервис
dc up -d                       # поднять всё после правки .env
dc down                        # остановить всё (данные в томах сохраняются)
```

После изменения `.env` на сервере выполни `dc up -d`: контейнеры с новыми настройками пересоздадутся.

## Откат на предыдущую версию

1. В GitHub → **Actions** найди последний успешный запуск и его коммит (sha).
2. На сервере:

   ```bash
   cd ~/tgtranslate
   sed -i 's/^TAG=.*/TAG=<полный_sha>/' .deploy.env
   dc pull && dc up -d
   ```

Либо сделай `git revert <коммит>` и запушь — откат пройдёт через обычный пайплайн. Это предпочтительнее.

Если образ приватный и `dc pull` требует авторизации, войди в GHCR с
[Personal Access Token](https://github.com/settings/tokens) с правом `read:packages`:
`echo <токен> | docker login ghcr.io -u esenin9426 --password-stdin`.

## Бэкап базы

```bash
dc exec -T postgres pg_dump -U tgt tgt | gzip > ~/backup_$(date +%F).sql.gz
```

Восстановление:

```bash
gunzip -c ~/backup_2026-10-07.sql.gz | dc exec -T postgres psql -U tgt tgt
```

Автоматический ежедневный бэкап в 03:00 (`crontab -e` под пользователем `deploy`):

```cron
0 3 * * * cd ~/tgtranslate && docker compose --env-file .env --env-file .deploy.env exec -T postgres pg_dump -U tgt tgt | gzip > ~/backup_$(date +\%F).sql.gz && find ~ -name 'backup_*.sql.gz' -mtime +14 -delete
```

## Если что-то пошло не так

| Симптом | Что проверить |
|---|---|
| `test` красный | Запусти локально `ruff check . && pytest`, почини, запушь снова |
| `deploy`: `Permission denied (publickey)` | `SSH_KEY` — приватный ключ целиком, включая строки `-----BEGIN/END-----`; публичный ключ лежит в `/home/deploy/.ssh/authorized_keys` |
| `deploy`: `permission denied ... docker.sock` | `deploy` не в группе `docker`: `sudo usermod -aG docker deploy` |
| `deploy`: `set POSTGRES_PASSWORD in .env` | Нет `.env` на сервере или в нём нет `POSTGRES_PASSWORD` (шаг 4) |
| `deploy`: `denied` при `pull` из ghcr.io | Repo → Settings → Actions → General → Workflow permissions: включи **Read and write permissions** |
| Бот молчит | `dc logs bot` — проверь `BOT_TOKEN`; не запущен ли бот где-то ещё |
| Источники зависают в обработке | `dc logs worker ollama`; не хватает RAM — поставь `OLLAMA_MODEL=qwen2.5:3b` и `dc up -d` |
| Сменил пароль Postgres, а база не пускает | Пароль задаётся только при первом создании тома. Поменяй его в базе: `dc exec postgres psql -U tgt -c "ALTER USER tgt PASSWORD '<новый>'"` |

## Из каких файлов это состоит

- `.github/workflows/deploy.yml` — пайплайн CI/CD.
- `docker-compose.prod.yml` — compose для сервера (CI копирует его туда как `docker-compose.yml`).
- `Dockerfile` — сборка образа.
- На сервере: `~/tgtranslate/.env` (твои секреты, CI его не трогает) и `~/tgtranslate/.deploy.env`
  (какой образ запущен, его пишет CI).
