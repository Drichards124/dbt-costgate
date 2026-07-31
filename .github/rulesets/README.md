<!-- SPDX-License-Identifier: Apache-2.0 -->

# Branch ruleset backups

What protects `main` and `ple`, captured as the exact payload GitHub's API
accepts back. These files are a **record and a restore path**, not a source of
truth: GitHub does not read them, and nothing applies them automatically. The
live rulesets are edited in the repository's Settings, and these are how you find
out what they used to say.

They exist because a ruleset is a handful of booleans, edited rarely, in a UI
that keeps no history. When one changes there is nothing to diff against and
nothing to revert to.

## Restoring one

The files are already in the shape `PUT` expects — `id`, timestamps, `_links` and
`current_user_can_bypass` are response-only and are rejected on the way back in,
so they are stripped here.

```bash
gh api --method PUT repos/Drichards124/dbt-costgate/rulesets/19635915 \
  --input .github/rulesets/protect-main.json
```

| Ruleset | id |
|---|---|
| `protect-main` | `19635915` |
| `protect-ple` | `19635906` |

## Checking these still match reality

Nothing does this for you. Diff a live ruleset against its file:

```bash
gh api repos/Drichards124/dbt-costgate/rulesets/19635915 \
  | jq '{name, target, enforcement, bypass_actors, conditions, rules}' \
  | diff - <(jq . .github/rulesets/protect-main.json) && echo "in sync"
```

Deliberately not a test. It needs a token and a network call, so in CI it would
either be skipped on forks and pull requests — the runs where most changes are
seen — or fail for reasons that have nothing to do with the change under review.
A check that is green because it was skipped is worse than no check.

## What these currently say

Both rulesets are `active` with **no bypass actors**, so nobody can merge past
them. Both block branch deletion and non-fast-forward pushes, and both require a
pull request.

| | `main` | `ple` |
|---|---|---|
| Required check | `PLE gate` | `CI gate` |
| Up-to-date with base required | no | no |
| Approvals required | 0 | 0 |

`PLE gate` is the one that matters on `main`: it is the full OS and Python matrix
plus the packaged-wheel and container smoke tests, and it must be green **on the
exact commit being merged**.

### Why `main` no longer requires branches to be up to date

It did until **2026-07-31**. Every `ple → main` promotion merges as a merge
commit, which lands on `main` and nowhere else — so `ple` was left one commit
behind the moment a promotion finished, and the *next* promotion opened as
`BEHIND` by construction. That cost a branch update per release and bought
nothing: `main` has no source of commits other than the promotion being merged,
so there is no third party to race with, and `PLE gate` already runs against the
exact head.

To put it back, set `strict_required_status_checks_policy` to `true` in
`protect-main.json` and `PUT` it with the command above.
