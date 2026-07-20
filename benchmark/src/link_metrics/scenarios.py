"""Executable metadata for the scored success-path Scenarios."""

SCENARIO_CONFIGURATIONS = {
    "registration": {
        "authentication": "none",
        "selection": "unique-seeded-registration-identities",
        "bodyValidation": "seeded-one-percent",
        "p99BudgetMs": 1_000,
    },
    "login": {
        "authentication": "seeded-credentials",
        "selection": "seeded-user-stream",
        "bodyValidation": "seeded-one-percent",
        "p99BudgetMs": 1_000,
    },
    "short-link-creation": {
        "authentication": "reference-token-corpus",
        "selection": "all-reference-users-evenly",
        "destinations": "byte-stable-per-iteration",
        "shortCodes": "database-generated",
        "bodyValidation": "seeded-one-percent",
        "p99BudgetMs": 250,
    },
    "uniform-resolution": {
        "authentication": "none",
        "selection": "all-seeded-short-links-evenly",
        "locationValidation": "every-response",
        "p99BudgetMs": 250,
    },
    "viral-resolution": {
        "authentication": "none",
        "selection": "ninety-percent-viral-ten-percent-uniform",
        "locationValidation": "every-response",
        "p99BudgetMs": 250,
    },
    "statistics": {
        "authentication": "reference-token-corpus",
        "selection": "owned-short-links-evenly-null-and-nonnull",
        "bodyValidation": "seeded-one-percent",
        "p99BudgetMs": 250,
    },
}

SCENARIOS = tuple(SCENARIO_CONFIGURATIONS)
PROTECTED_SCENARIOS = frozenset(
    scenario
    for scenario, configuration in SCENARIO_CONFIGURATIONS.items()
    if configuration["authentication"] == "reference-token-corpus"
)
P99_BUDGETS_MS = {
    scenario: int(configuration["p99BudgetMs"])
    for scenario, configuration in SCENARIO_CONFIGURATIONS.items()
}
