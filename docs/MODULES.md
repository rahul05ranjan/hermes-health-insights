# Optional modules

Core checks (blood pressure, vitals, sleep, labs, workouts, data gaps, nutrition gaps) always run. **Modules** are extra checks and skills for particular situations. They are all **off by default**; you turn on only what applies to you.

## Turn modules on and off
```bash
health-insights modules list                 # every module, [on ] / [off], one line each
health-insights modules info glp1 --db my.sqlite   # what it does, data it needs, cautions, what your database has
health-insights modules enable glp1
health-insights modules disable glp1
```
Switches live in `~/.config/health-insights/modules.yaml`, a tiny file the tool owns, so your own `config.yaml` comments are never rewritten. Edit it by hand if you prefer:
```yaml
glp1: true                 # on
medication_adherence: false
```
Mapping form, with options: `glp1: {enabled: true, options: {weight_pct_per_week: 1.5}}`. You can also put a `modules:` block in `config.yaml`, or enable modules for one run with `HEALTH_INSIGHTS_MODULES=glp1,medication_adherence`. Precedence: environment, then `modules.yaml`, then `config.yaml`.

Ask your agent: the `health-insights-modules` skill lists modules in plain language, explains cautions before enabling, and only turns one on when you ask.

## Adding a module (for contributors)
A module is one entry in `health_insights/modules.py` plus its rules and skill:
1. Write the rules in `health_insights/concern_rules/<name>_rules.py` as functions `f(db_path, ref_date) -> list[Finding]`. Use only aggregates and your own baselines; word findings as information, "worth watching", or "discuss with your clinician", and urgent findings as "seek prompt medical care". Never give dosing or medication-change advice (`modules.lint_advice()` rejects that wording in tests).
2. Register it: `register(Module(id=..., title=..., summary=..., category=..., data_needs=(type_codes...), rules=((name, fn),), skills=(skill ids...), cautions=(...)))`. `default_enabled` stays False. `summary` is one plain sentence saying what turning it on does.
3. Write tests first: the rule with synthetic data, the off-by-default behaviour, and a `lint_advice` check over every finding string. `tests/test_modules.py` already enforces the contract for every registered module (id format, summary, at least one caution, off by default).
4. Add the skill under `public/skills/` and list it in the module's `skills`. Update this document's module table and the README.
5. Publish with `tools/publish_public.py` (see the private repo's `docs/PUBLISHING.md`).

## Current modules
| id | What it does | Needs |
| --- | --- | --- |
| `glp1` | Watches your own data for patterns worth knowing about on a GLP-1 type medicine, plus warning signs. Details: [GLP1_MONITORING.md](GLP1_MONITORING.md) | weight, resting heart rate, food log; blood pressure and glucose if you have them |
| `medication_adherence` | Flags repeated missed doses from a separate `medlog` tool (counts only) | the `medlog` command line tool (separate, not included with this plugin) |
