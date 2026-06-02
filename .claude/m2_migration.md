    Migration strategy — strangler, not big-bang

    1. Add models.py first; have existing functions return dataclasses while keeping signatures. Tests
    stay green.
    2. Introduce Repository, move SQL into it method-by-method, point callers at it.
    3. Wrap each parser in its class; extract the shared run_full loop into base.py last (once both
    parsers behave identically at scale).
    4. Split resolver into resolve/ package — this is the biggest win for readability.
    5. Delete the old module shims.

    Do each step as its own commit with the suite green — feat:/refactor: per your Conventional Commits
    rule. Because nothing changes behavior, the coverage report output before and after is your regression
    oracle.

    One caution consistent with where the project is: the base-parser extraction (step 3) is the one piece
    I'd still hold until after the full Latin scan, because the anomaly fixes will tell you what the real
    base-class contract is. Everything else (models, repository, deepseek client, resolver split) you can
    do now without that risk.
