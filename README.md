# skill-sync

Одна команда, которая по YAML-манифесту раскладывает [Agent Skills](https://code.claude.com/docs/en/skills) в проект: скачивает источники (GitHub / npx / локальные) в общий кэш и создаёт симлинки в `.agents/skills/`.

## Как это работает

1. Читается манифест — список нужных skills.
2. Каждый внешний источник скачивается в общий кэш (`.skill-sources/`) и переиспользуется всеми проектами.
3. На каждый skill создаётся симлинк `<project>/.agents/skills/<name>` → папка с `SKILL.md` внутри источника.
4. `<project>/.claude/skills` линкуется на `../.agents/skills` — Claude Code читает skills именно оттуда, а не из `.agents/skills/` напрямую.
5. По умолчанию — сухой прогон (печатается план). Запись на диск только с флагом `--apply`.

Скрипт не перезаписывает настоящие каталоги и чужие симлинки ни в `.agents/skills/`, ни в `.claude/skills`.

## Требования и установка

- Python ≥ 3.9
- пакет `pyyaml`
- `git` — для источников `github.com:…`
- `node` + `npx` — только если используются источники `npx/…`

Проект собирается через `hatchling` (см. `pyproject.toml`). Варианты:

```bash
# uv: поставить как консольную команду `skill-sync`
uv tool install .
skill-sync --project /path/to/project --apply

# или dev-окружение
uv sync            # + dev-группа: pytest, ruff, mypy
uv run skill-sync --project /path/to/project --apply

# или pip
pip install -e .
skill-sync --project /path/to/project --apply
```

Можно и без установки — это один файл, зависящий только от `pyyaml`:

```bash
pip install pyyaml
python3 skill_sync.py --project /path/to/project --apply
```

Точка входа `skill-sync` эквивалентна `python3 skill_sync.py`.

## Манифест

YAML-файл. По умолчанию берётся `config.yaml` из каталога `--project`; другой путь — через `--config`.

```yaml
skills:
  common:
    - github.com:addyosmani/agent-skills:all      # все skills из репозитория
    - github.com:owner/repo:some-skill            # один конкретный skill
    - personal:my-skill                           # из custom-skills/my-skill/ рядом со скриптом
    - npx/ecc-universal:ecc-guide                 # селективная установка через npx
  codex: []
  claude: []
```

Секции `common`, `codex`, `claude` объединяются в один общий пул (без фильтрации по агенту).

Формы записи источника:

| Форма | Значение |
| --- | --- |
| `github.com:<owner/repo>:<skill>` | один skill из репозитория |
| `github.com:<owner/repo>:all` | все найденные skills репозитория |
| `personal:<name>` | локальный skill из `custom-skills/<name>/` рядом со скриптом |
| `npx/<package>:<skill>` | `npx <package> install --skills <skill>`, переносится только этот skill |

Поддерживается развёрнутая форма:

```yaml
skills:
  common:
    github.com:
      - owner/repo:
          - skill-a
          - skill-b
    personal:
      - my-skill
```

`github` — синоним `github.com` (только как ключ в развёрнутой форме; в строковом
селекторе нужен полный `github.com:`). `all` с `npx` не поддерживается.

Имя skill берётся из имени папки с `SKILL.md`. Источник сканируется в подкаталоге `skills/`, если он есть, иначе от корня. Недопустимые имена папок и дубликаты имён приводят к ошибке.

## Применение

```bash
# план: ничего не качает и не пишет
python3 skill_sync.py --project /path/to/project

# скачать источники и создать симлинки
python3 skill_sync.py --project /path/to/project --apply

# подтянуть обновления уже скачанных источников (git pull --ff-only / переустановка)
python3 skill_sync.py --project /path/to/project --apply --update

# убрать симлинки, которых больше нет в манифесте (только созданные этим инструментом)
python3 skill_sync.py --project /path/to/project --apply --prune
```

| Флаг | Назначение |
| --- | --- |
| `--project <path>` | целевой проект; по умолчанию — текущая папка |
| `--config <path>` | путь к манифесту; по умолчанию `<project>/config.yaml` |
| `--apply` | единственный флаг, который скачивает и пишет на диск |
| `--update` | обновить кэш и подхватить skills, добавленные в источник после прошлого запуска |
| `--prune` | удалить из `.agents/skills/` управляемые симлинки, пропавшие из манифеста |
| `--cache-dir <path>` | где хранить кэш источников (см. ниже) |

## Общий кэш источников

Скачанные GitHub/npx-источники лежат в одном кэше и переиспользуются всеми проектами.
Расположение (в порядке приоритета):

1. флаг `--cache-dir <path>`
2. переменная окружения `SKILL_SYNC_CACHE`
3. по умолчанию — `.skill-sources/` рядом со `skill_sync.py`

```bash
export SKILL_SYNC_CACHE="$HOME/.cache/agent-skills"
```

## Что появляется в проекте

```text
.agents/skills/<name>       # симлинк на папку со SKILL.md внутри источника
.claude/skills               # симлинк -> ../.agents/skills, чтобы skills видел Claude Code
```

Кэш (`.skill-sources/<source>/<owner%2Frepo>/`) лежит в общем месте, а не в проекте.

В `.gitignore` целевого проекта стоит добавить:

```gitignore
.agents/skills/
.claude/skills/
```

## Поведение при повторных запусках

- Нужный симлинк уже на месте → пропускается (идемпотентно).
- По целевому пути лежит настоящий файл или чужой симлинк → ошибка, ничего не меняется.
- Источник ещё не скачан и запуск без `--apply` → строка `Missing source (run with --apply)`.
- Для `github.com` при `--update`: сначала `git pull --ff-only`, при неудаче — повторное клонирование.
