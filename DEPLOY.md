# Deployment Policy

## Deploy = push -> PR -> merge, end-to-end

When asked to fully deploy, complete the full chain in one pass:

1. Commit - stage and commit the change with a clear message.
2. Push - push the branch to origin.
3. PR - open a pull request against the default branch.
4. Merge - merge the PR.
5. Verify - confirm the commit landed on the default branch (e.g. git log origin/main).

## Rules

- Never stop at push or leave a PR open when a full deploy was requested.
- If a full deploy is requested, do not ask for confirmation before merging - merging is part of the definition.
- Individual steps (commit, push, PR, merge) may be requested separately and should stop at that step only.
- Always verify the final commit is present on the default branch before reporting completion.
- If merge is blocked (required checks, conflicts, review requirements), report the blocker explicitly - do not silently stop and call it done.

## Related terms

| Term | Meaning | Scope |
|---|---|---|
| commit | commit | local only |
| push | push | commit + push |
| PR | open pull request | commit + push + PR |
| merge | merge | merge only (assumes PR exists) |
| deploy | deploy | commit + push + PR + merge + verify |