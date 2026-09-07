# Contributing: the workflow contract

Follow it and reviews stay fast. Each rule exists because breaking it once
cost real time (see `docs/DECISIONS.md`).

## 1. Plan → critiquer → implement

Non-trivial change: write the plan, run the `critiquer` subagent on it, merge
the verdict before coding. Scope calls the critiquer can't make (drop vs
rescope a stream, timing of i18n) go to the owner via one grouped question —
never silently decided.

## 2. One stream, one branch, one PR

Branches `feat/s*` off the previous tip; each PR targets `mannangrover:master`
from `404Mayank:<branch>` and notes its stack position ("review only the top
commit"). Keep PRs reviewable: tree surgery, provider, query type, data, RAG
and test-env are six PRs, not one diff.

## 3. Green per commit, proven by stash A/B

`pytest` must pass on every commit. New failures are proven absent by diffing
the failure set with and without the change (`git stash`, run, compare,
`git stash pop`). Pre-existing failures stay byte-identical or get fixed in
the test-env stream — never silently absorbed.

## 4. Tests prove contracts, not vibes

New behavior ships with tests that fail without the code (check by stashing).
Number-bearing claims need verifier coverage; fences need both-directions
tests (the bypass AND the legitimate path); fallbacks need exhaustion tests.
Live services are never touched from tests (`conftest.py::hermetic_env`).

## 5. Small, honest diffs

- Prompts live in `agents/prompts/*.md`, never inline. The tool block is
  injected from the registry — never hand-list tools in a prompt.
- Frontend types are generated (`scripts/gen_types.py`); `tsc -b` must pass in
  any commit touching schemas.
- Ruff clean on touched files (`ruff check <files>`); repo-wide debt is not
  yours to fix in a feature PR.
- Commit messages state what, why, and the measured proof (suite counts,
  latencies, verification method). "Verified live" means against the real
  endpoint with quota — roster presence doesn't count.
- Docs change with behavior: `ARCHITECTURE.md`/`CLAUDE.md` for contracts,
  `docs/DECISIONS.md` for reversals, `PROGRESS.md` numbers only when measured.

## 6. Never commit

`data/` (derived cache), `.env` (secrets), `.venv/`, `node_modules/`,
`frontend/dist`, hand-drawn boundary polygons, unverified coordinates,
model ids not seen on a live roster. When in doubt, the degraded honest
version ships; faked data never does.
