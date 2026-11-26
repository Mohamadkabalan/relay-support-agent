"""The eval harness (PLAN.md §9): simulated conversations graded on the final database state.

- `scenario`: the YAML scenario format, loading and validation.
- `databases` and `fixtures`: the template database, one clone per trial, scenario fixtures.
- `simulator` and `trial`: the simulated customer, the operator stub and one trial end to end.
- `grading`: database diff, conversation checks, terminal state, violations and blocked attempts.
- `runner`, `metrics`, `report`: a whole run, its statistics and `report.md` / `report.json`.
- `selftest`: checks of the harness itself that make no model calls.
"""
