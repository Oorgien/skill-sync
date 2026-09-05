# Agent Skills Hub

Личный репозиторий для управления Agent Skills, совместимых с Codex и Claude Code.

## Что находится в репозитории

- `custom-skills/` — собственные skills; это основной редактируемый источник.
- `ECC/` — внешний набор Everything Claude Code, подключённый как Git submodule.
- `skills/` — внешний набор `mattpocock/skills`, подключённый как Git submodule.
- `catalog/skills.json` — машиночитаемый каталог с описаниями и оценками.
- `docs/catalog/` — человекочитаемая документация по всем skills.
- `manifests/` — декларативные наборы skills для отдельных проектов.

Полный каталог начинается с [docs/catalog/README.md](./docs/catalog/README.md).

## Получение репозитория

```bash
git clone --recurse-submodules <url-вашего-репозитория>
```

Для уже клонированного репозитория:

```bash
git submodule update --init --recursive
```

## Обновление внешних наборов

```bash
git submodule update --remote ECC skills
npm run catalog:refresh
```

После обновления проверьте новые или удалённые skills. Новые записи получают пустую оценку до ручного ревью, поэтому `npm run verify` не пропустит их незаметно.

## Пересборка документации

```bash
npm run catalog
npm run verify
```

`npm run catalog` пересобирает Markdown из сохранённого JSON. `npm run catalog:refresh` сначала повторно сканирует submodules, сохраняя существующие ручные оценки.

## Собственные skills

Создавайте каждый skill в отдельном каталоге:

```text
custom-skills/
└── my-skill/
    ├── SKILL.md
    ├── scripts/
    └── references/
```

Минимальный переносимый frontmatter:

```yaml
---
name: my-skill
description: Когда применять skill и какой результат он даёт.
---
```

Для максимальной совместимости общую логику следует держать в стандартном `SKILL.md`, а специфичные для одного harness возможности добавлять только при реальной необходимости.
