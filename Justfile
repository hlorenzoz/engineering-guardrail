# engineering-guardrail: shared git hooks. Run `just` to list recipes.

default:
    @just --list

# The direct tests of the pure readers and of the SQL reader, then the canary scenarios: every lock, through the real command line
test:
    sh tests/log.sh unittest python3 -m unittest -v tests.test_units tests.test_schema tests.test_scenarios

# Break each lock on purpose; the scenarios must turn red (80 s measured on 2026-10-16 at 3667b94: 234 mutants, 3 workers, control run included; 30.6 min before the killer-first order)
mutants:
    sh tests/log.sh mutants python3 -m unittest -v tests.test_mutants

# Everything that must pass before a release
check: test mutants
