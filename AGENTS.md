# Agent Skills Hub instructions

## Ownership

- `custom-skills/` is the canonical editable source for personal skills.
- `ECC/` and `skills/` are upstream Git submodules. Do not edit their contents unless the user explicitly asks to contribute to that upstream repository.
- `catalog/skills.json` contains source metadata plus reviewed usefulness ratings.
- `docs/catalog/*.md` is generated from `catalog/skills.json`.

## Catalog workflow

1. After a submodule update, run `npm run catalog:refresh`.
2. Review every entry with a missing score or Russian summary.
3. Run `npm run catalog` after changing ratings.
4. Run `npm run verify` before committing catalog changes.

Never assign a score automatically only from file length or keyword counts. A usefulness rating must consider applicability, workflow completeness, uniqueness, external dependencies, and maintenance risk.

## Skill compatibility

Prefer the common Agent Skills format. Every personal skill must have a `SKILL.md` with clear `name` and `description` fields. Avoid hardcoded secrets and machine-specific paths.
