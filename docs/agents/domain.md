# Domain Docs

How engineering skills should consume this repository's domain documentation.

## Before exploring, read these

- **`CONTEXT.md`** at the repository root.
- **`docs/adr/`** for decisions affecting the area being changed.

If these files do not exist, proceed silently. The `/domain-modeling` skill
creates them lazily when terminology or architectural decisions are resolved.

## File structure

This repository uses a single-context layout:

```
/
├── CONTEXT.md
└── docs/
    └── adr/
        ├── 0001-example-decision.md
        └── 0002-another-decision.md
```

## Use the glossary's vocabulary

When output names a domain concept, use the term defined in `CONTEXT.md`. Do
not drift to synonyms the glossary explicitly avoids.

If a needed concept is absent, reconsider whether the term belongs to the
project or note the gap for `/domain-modeling`.

## Flag ADR conflicts

If proposed work contradicts an existing ADR, surface the conflict explicitly
instead of silently overriding the decision.
