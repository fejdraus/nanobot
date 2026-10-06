# Бот-ревьюер MR GitLab на nanobot: установка на Linux-ПК

Бот принимает вебхуки GitLab, запускает ревью через Claude Code CLI (`claude -p`) скиллом
`review-gitlab-mrs`, присылает **черновик** в Telegram и публикует в GitLab только то, что вы
одобрили ответом «публикуй». Код: канал `nanobot/channels/gitlab_review/`, провайдер
`nanobot/providers/claude_cli_provider.py`. Подробности настроек: `docs/configuration.md` →
разделы «GitLab Review» и «Claude Code CLI».

Обозначения ниже: `<USER>` — пользователь Linux, `<HOST>` — имя машины в tailnet.

---

## 0. Что должно быть готово заранее

| Что | Где взять |
|---|---|
| Подписка Claude (Pro/Max) | ваша; на сервер переносится токеном `claude setup-token`, не файлами входа |
| Telegram-бот **только для ревьюера** | @BotFather → `/newbot` → токен вида `123456:ABC…` |
| Ваш Telegram chat id | ваш user id (для личного чата с ботом он же chat id) |
| Токен GitLab для публикации | Personal Access Token учётки, от которой идут комментарии, scope `api` |
| Токен GitLab для клона | Deploy token проекта или PAT с `read_repository` |
| Права в проекте GitLab | Maintainer — чтобы добавить вебхук |
| Tailscale на Linux-ПК | для публичного адреса вебхука (Funnel) |

> Условия подписки (проверено 05.10.2026): headless-режим `claude -p` и `claude setup-token`
> документированы для скриптов/CI; вход своей подпиской в неизменённый `claude` разрешён.
> Бот — ваш инструмент: черновики одобряете вы и публикуете от своего имени. Если он начнёт
> обслуживать запросы других людей — переходить на API-ключ. Лимиты подписки общие с вашей
> обычной работой в Claude Code.

---

## 1. Код nanobot

Изменения бота должны попасть в форк, а с него — на ПК.

На Windows (`W:\GitHub\Bots\nanobot`):

```bash
git status                     # новые: nanobot/channels/gitlab_review/, providers/claude_cli_provider.py, tests/...
git add nanobot/channels/gitlab_review nanobot/providers/claude_cli_provider.py \
        nanobot/config/schema.py nanobot/providers/factory.py nanobot/providers/registry.py \
        tests/channels/gitlab_review tests/providers/test_claude_cli_provider.py \
        tests/channels/test_channel_setup.py docs/configuration.md docs/providers.md docs/automations.md
git commit -m "feat(gitlab_review): draft MR reviews, publish on Telegram approval"
git push origin main
```

На Linux-ПК (если nanobot уже стоит, как на junior — `~/test-claws/nanobot`):

```bash
cd ~/test-claws/nanobot
git pull origin main
.venv/bin/pip install -e .     # только если менялся pyproject.toml
```

Проверка тестами — только точечно и с ограничением памяти (полный прогон однажды положил junior):

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
ulimit -n 65536
mkdir -p ~/tt
systemd-run --user --scope -p MemoryMax=4G \
  .venv/bin/python -m pytest -q --basetemp=$HOME/tt \
  tests/channels/gitlab_review tests/providers/test_claude_cli_provider.py
rm -rf ~/tt
```

---

## 2. Claude Code CLI на ПК

```bash
npm install -g @anthropic-ai/claude-code      # или способ установки из документации Claude Code
claude --version
claude setup-token                            # один раз, откроет ссылку для входа; выведет токен
```

Токен сохраните в файл окружения сервиса (п. 7), переменная `CLAUDE_CODE_OAUTH_TOKEN`.
Не копируйте `~/.claude/.credentials.json` с Windows.

Перенесите в `~/.claude` на ПК:

- скилл `review-gitlab-mrs` (папка скилла из `C:\Users\<you>\.claude-settings\...\skills\` или
  `.claude/commands` проекта — откуда он у вас вызывается);
- глобальные инструкции, которые нужны ревью (`rules/`, `CLAUDE.md`), без локальных путей Windows.

**Память и уроки ревью.** Сейчас они в профиле Windows
(`C:\Users\fejdraus\.claude-settings\projects\R--Web-AstanaMotors-Terrasoft-WebApp-Terrasoft-Configuration\memory\`).
На ПК Claude Code заведёт свою папку по пути клона (п. 3), например
`~/.claude/projects/-home-<USER>-MotorsGit/memory/`. Скопируйте туда текущие файлы памяти. Дальше
решите: синхронизировать (git-репозиторий памяти, pull/push с обеих сторон) или вести раздельно —
иначе уроки бота и ваши разойдутся.

---

## 3. Клон проекта

```bash
cd ~
git clone https://<deploy-user>:<deploy-token>@gitlab.banzait.com/astana-group/astana-motors.git MotorsGit
cd MotorsGit && git checkout test
```

Скилл читает ревизии через `git show origin/<branch>:<path>` и `git fetch` — рабочее дерево не
трогает. Бот запускает ревью строго по одному, поэтому один клон безопасен.

В `MotorsGit` положите проектный `CLAUDE.md`/`.claude/` из рабочего репозитория, если скилл на них
опирается.

---

## 4. MCP-серверы для Claude Code

Нужны те же, что на Windows: `gitlab`, `clickup`, `jira` (Node/TypeScript из `W:\GitHub\`).

```bash
mkdir -p ~/mcp && cd ~/mcp
# скопировать W:\GitHub\gitlab-mcp-server, clickup-mcp, jira-mcp-server (без node_modules)
for d in gitlab-mcp-server clickup-mcp jira-mcp-server; do (cd $d && npm ci && npm run build); done
```

`.env` каждого сервера — как на Windows (URL и токены; GitLab-токен здесь может быть только на
чтение — публикует не MCP, а канал бота).

Регистрация в Claude Code (user scope, чтобы работало в любом `cwd`):

```bash
claude mcp add --scope user gitlab -- node ~/mcp/gitlab-mcp-server/dist/server.js
claude mcp add --scope user clickup -- node ~/mcp/clickup-mcp/dist/server.js
claude mcp add --scope user jira    -- node ~/mcp/jira-mcp-server/dist/server.js
claude mcp list
```

**Что будет недоступно с ПК:** `clio` (стенд `astanamotors-dev`, `http://localhost:8005`) и базы
`mssql-live` / `mssql-test` живут на Windows-машине. Без них бот не проверит коробочные схемы и
данные; остальное ревью работает. Если нужны — открыть их по Tailscale (работает, только пока
Windows-машина включена) и зарегистрировать MCP с адресами tailnet.

---

## 5. Конфиг экземпляра nanobot

```bash
nanobot onboard --config ~/.nanobot-reviewer/config.json --workspace ~/.nanobot-reviewer/workspace
```

Затем привести `~/.nanobot-reviewer/config.json` к виду (секреты — через переменные окружения
из п. 7):

```json
{
  "agents": {
    "defaults": {
      "model": "claude_cli/claude-opus-5-5",
      "workspace": "~/.nanobot-reviewer/workspace"
    }
  },
  "providers": {
    "claude_cli": {
      "cliPath": "claude",
      "cwd": "/home/<USER>/MotorsGit",
      "timeoutS": 3600,
      "allowedTools": [
        "Read(/home/<USER>/MotorsGit/**)", "Read(~/.claude/**)", "Grep", "Glob",
        "Read(/home/<USER>/.nanobot-reviewer/gitlab_review/archive/**)",
        "Bash(git fetch:*)", "Bash(git show:*)", "Bash(git log:*)", "Bash(git diff:*)",
        "Bash(git grep:*)", "Bash(git ls-remote:*)", "Bash(git rev-list:*)", "Bash(git merge-base:*)",
        "Write(~/.claude/projects/**)", "Edit(~/.claude/projects/**)",
        "mcp__gitlab__gitlab_list_mrs", "mcp__gitlab__gitlab_mr_status",
        "mcp__gitlab__gitlab_list_mr_notes", "mcp__gitlab__gitlab_list_draft_notes",
        "mcp__gitlab__gitlab_list_mr_uploads", "mcp__gitlab__gitlab_download_upload",
        "mcp__gitlab__gitlab_mrs_by_jira",
        "mcp__clickup__clickup_find_by_jira_key", "mcp__clickup__clickup_get_task",
        "mcp__clickup__clickup_attachments", "mcp__clickup__clickup_attachment_get",
        "mcp__jira__jira_get", "mcp__jira__jira_attachments", "mcp__jira__jira_attachment_get"
      ],
      "disallowedTools": [
        "mcp__gitlab__gitlab_create_mr_discussion", "mcp__gitlab__gitlab_create_mr_note",
        "mcp__gitlab__gitlab_reply_to_mr_discussion", "mcp__gitlab__gitlab_approve_mr",
        "mcp__gitlab__gitlab_unapprove_mr", "mcp__gitlab__gitlab_resolve_thread",
        "mcp__gitlab__gitlab_merge_mr", "mcp__gitlab__gitlab_close_mr",
        "mcp__gitlab__gitlab_update_mr", "mcp__gitlab__gitlab_create_mr",
        "mcp__gitlab__gitlab_update_mr_note", "mcp__gitlab__gitlab_delete_mr_note",
        "mcp__gitlab__gitlab_set_mr_labels",
        "mcp__jira__jira_add_comment", "mcp__jira__jira_transition", "mcp__jira__jira_assign",
        "mcp__clickup__clickup_add_comment", "mcp__clickup__clickup_set_status",
        "mcp__clickup__clickup_update_task"
      ]
    }
  },
  "channels": {
    "websocket": { "port": 8771 },
    "gitlab_review": {
      "enabled": true,
      "webhookSecretToken": "${GITLAB_WEBHOOK_TOKEN}",
      "host": "127.0.0.1",
      "port": 3980,
      "webhookPath": "/gitlab/webhook",
      "projectPath": "astana-group/astana-motors",
      "gitlabUrl": "https://gitlab.banzait.com",
      "gitlabToken": "${GITLAB_REVIEW_TOKEN}",
      "reviewerUsernames": ["a.tyra"],
      "reviewOwnMergeRequests": true,
      "clickupToken": "${CLICKUP_TOKEN}",
      "clickupTeamId": "9015049156",
      "jiraUrl": "https://boards.banzait.com",
      "lessonsDir": "/home/<USER>/.claude/projects/-home-<USER>-MotorsGit/memory",
      "telegramBotToken": "${REVIEW_TELEGRAM_BOT_TOKEN}",
      "telegramChatId": "49816954",
      "telegramUserIds": ["49816954"],
      "debounceSeconds": 90,
      "maxRunsPerMrPerHour": 6
    }
  },
  "gateway": { "port": 18797 }
}
```

Пояснения:

- `disallowedTools` — обязательная защита: модель не должна публиковать сама, публикует только
  канал после «публикуй». Названия инструментов сверить с `claude mcp list` / `/mcp` на ПК.
- Синтаксис правил `Write(...)`/`Edit(...)` для пути памяти сверить с документацией Claude Code
  (permissions); без них бот не сможет записывать уроки, но ревью работать будет.
- `channels.websocket.port` — уникальный: на junior заняты 8765–8770
  (`grep -rn "87[67][0-9]" ~/.nanobot-*/config.json`). `gateway.port` — тоже свободный.
- `host: 127.0.0.1` (это и значение по умолчанию) — наружу порт открывает Funnel (п. 6), сам
  листенер в сеть не смотрит.
- `Read` ограничен клоном и `~/.claude`: модель читает текст MR от посторонних людей, и без
  ограничения её можно уговорить прочитать `reviewer.env`. Секреты сервиса в окружение `claude`
  и так не попадают — провайдер передаёт ему только `PATH`, `HOME`, локаль, прокси и `CLAUDE_*`.
- `telegramUserIds` — кто может одобрять. Для личного чата достаточно `telegramChatId` (одобрять
  может только его владелец); для группового чата список обязателен.
- Общий Telegram-канал nanobot в этом экземпляре **не включать**: у канала ревью свой бот.

Проверка конфига:

```bash
nanobot status --config ~/.nanobot-reviewer/config.json
```

---

## 6. Публичный адрес вебхука (Tailscale Funnel)

```bash
sudo tailscale funnel --bg 3980
tailscale funnel status          # покажет https://<HOST>.<tailnet>.ts.net/
```

Адрес вебхука: `https://<HOST>.<tailnet>.ts.net/gitlab/webhook`.

---

## 7. Сервис systemd

Файл секретов `~/.nanobot-reviewer/reviewer.env` (права `600`):

```bash
CLAUDE_CODE_OAUTH_TOKEN=...          # из claude setup-token
GITLAB_WEBHOOK_TOKEN=...             # длинная случайная строка: openssl rand -hex 32
GITLAB_REVIEW_TOKEN=...              # PAT учётки-ревьюера, scope api
REVIEW_TELEGRAM_BOT_TOKEN=...        # от @BotFather
CLICKUP_TOKEN=...                    # токен ClickUp (как у MCP clickup) — название и ссылка задачи в черновиках
```

```bash
chmod 600 ~/.nanobot-reviewer/reviewer.env
nanobot gateway install-service --manager systemd --name nanobot-reviewer \
  --config ~/.nanobot-reviewer/config.json --workspace ~/.nanobot-reviewer/workspace
systemctl --user edit nanobot-reviewer
```

В открывшийся override добавить:

```ini
[Service]
EnvironmentFile=%h/.nanobot-reviewer/reviewer.env
```

```bash
loginctl enable-linger $USER
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user restart nanobot-reviewer
systemctl --user is-active nanobot-reviewer
journalctl --user -u nanobot-reviewer -n 50
```

Если в логе «port … in use» — сменить `channels.websocket.port`/`gateway.port`.

---

## 8. Вебхук в GitLab

Проект `astana-group/astana-motors` → Settings → Webhooks → Add new webhook:

- URL: `https://<HOST>.<tailnet>.ts.net/gitlab/webhook`
- Secret token: значение `GITLAB_WEBHOOK_TOKEN`
- Triggers: **Merge request events**, **Comments**
- SSL verification: включено

Кнопка **Test → Merge request events**: в логе сервиса не должно быть `invalid token`; ответ
`{"ok":true,...}`.

---

## 9. Первый запуск

1. Напишите своему новому боту в Telegram `/start` (сообщения из других чатов игнорируются молча).
2. Дождитесь нового MR или переоткройте любой открытый. Свои MR бот тоже ревьюит
   (`reviewOwnMergeRequests: true`), но аппрув для них не предлагает и не публикует.
3. Через ~90 с после события начнётся ревью; черновик придёт в Telegram с нумерацией действий.
   Ревёрты пропускаются: если все коммиты MR — ревёрты («This reverts commit …» / «This reverts merge
   request !N»), бот только сообщает об этом. Проверить любой MR вручную: «проверь !N».
4. Решение по черновику — `публикуй` (всё), `публикуй 1,3` (выборочно) или `отмена`:
   - ответом (reply) на сообщение черновика или на любой ответ бота по этому ревью;
   - без ответа — если черновик в ожидании один; если их несколько, бот их перечислит;
   - командой с номером: `публикуй !<iid>/<версия>`, `публикуй !<iid>/<версия> 1,3`, `отмена !<iid>`.
5. Всё остальное — разговор с ревьюером: «почему считаешь, что флаг не сбрасывается?», «убери
   второе замечание», «проверь ещё descriptor.json».
   - Разговор идёт в сессии Claude того ревью, о котором он: ответом на его сообщение или, без
     ответа, о единственном ожидающем черновике. Бот помнит, что читал и почему так решил.
   - Каждое новое ревью (и повторное ревью того же MR) начинается с новой сессии.
   - Бот может ответить, выпустить исправленный черновик (новая версия `iid/2`) или понять, что
     вы просите опубликовать или отменить — любыми словами («выкатывай», «отправь», «ок, давай»).
     Публикуется черновик, который вы видели; бот пишет, по какой вашей фразе он это сделал.
   - Пока бот думает, в чате видно «печатает…». Сообщения обрабатываются по очереди с ревью.
6. Архив: каждое ревью, черновики, ваши решения и переписка сохраняются. Вопрос о прошлом ревью
   («что было по AMCRM-16127?», «что ответили по !6318?») бот находит по номеру задачи или MR,
   а по теме — поиском по каталогу архива (по файлу Markdown на ревью).
7. Профили разработчиков — как Dream в nanobot, в два шага:
   - после ревью, ответа в треде или разговора бот может записать наблюдения об участниках, но
     это лишь улики в архиве, в профиль они сразу не попадают;
   - раз в сутки (в `peopleDreamHour`, по умолчанию 4:00, и один раз при первом запуске) бот для
     каждого разработчика, у кого появилось новое, перечитывает профиль, улики за
     `peopleHistoryDays` и собственные сообщения разработчика в ваших тредах и пересобирает
     профиль `people/<логин>.md`: «Общение» (язык, тон, как принимает замечания, что помогает
     понять), «Привычки в коде» (только повторившиеся в двух MR и больше), «Сильные стороны».
     Обычная работа и разовые эпизоды не записываются. Изменения приходят в Telegram.
   Перед ревью в промпт идёт профиль автора MR, перед ответом в треде — и того, кто ответил.
   Код отбрасывает оценки характера и профиль, сломавший разделы или размер. Кто не дал
   согласия — в `peopleExcluded` (логины GitLab): о них не пишут и профиль не показывают.
8. Если ветку обновили после ревью, публикация откажет — дождитесь нового ревью. Если пришёл
   новый черновик того же MR, команда со старой версией (или без версии) тоже откажет.
9. Ревью дольше `reviewTimeoutS` останавливается, поздний ответ черновиком не становится.

Что бот делает сам без вас: ничего не публикует. Пишет вам в Telegram, если вас упомянули в чужом
треде, если неясно, кому адресован ответ, при превышении лимита запусков (6 в час на MR), при
ошибке ревью или команды.

---

## 10. Сначала обкатать на Windows (необязательно)

Всё то же, но: `cwd` = `R:\MotorsGit`, MCP и стенд уже есть, `setup-token` не нужен (CLI уже
вошёл), запуск `nanobot gateway --config %USERPROFILE%\.nanobot-reviewer\config.json`, Funnel —
`tailscale funnel --bg 3980` на Windows. Минусы: работает только пока ПК включён, лимиты подписки
делятся с вашей текущей работой.

---

## Обслуживание

| Задача | Команда |
|---|---|
| Логи | `journalctl --user -u nanobot-reviewer -f` |
| Перезапуск после правки конфига | `systemctl --user restart nanobot-reviewer` |
| Обновление кода | п. 1 (pull) + restart |
| Состояние (дедуп, черновики, лимиты, архив) | `~/.nanobot-reviewer/.../gitlab_review/state.sqlite3` |
| Архив ревью для чтения и поиска | `~/.nanobot-reviewer/.../gitlab_review/archive/*.md` |
| Отключить | `systemctl --user stop nanobot-reviewer`, вебхук в GitLab — Disable |
