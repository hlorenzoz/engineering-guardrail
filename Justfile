# engineering-guardrail: shared git hooks. Run `just` to list recipes.

default:
    @just --list

# The direct tests of the pure readers, then the canary scenarios: every lock, through the real command line
test:
    sh tests/log.sh unittest python3 -m unittest -v tests.test_units tests.test_scenarios

# Break each lock on purpose; the scenarios must turn red (29 minutes measured on 2026-10-15 at 8253b9c: 156 mutants, 3 workers)
mutants:
    sh tests/log.sh mutants python3 -m unittest -v tests.test_mutants

# Everything that must pass before a release
check: test mutants
