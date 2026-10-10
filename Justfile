# engineering-guardrail: shared git hooks. Run `just` to list recipes.

default:
    @just --list

# The canary scenarios: every lock, through the real command line
test:
    python3 -m unittest -q tests.test_scenarios

# Break each lock on purpose; the scenarios must turn red (about 4 minutes)
mutants:
    python3 -m unittest -q tests.test_mutants

# Everything that must pass before a release
check: test mutants
