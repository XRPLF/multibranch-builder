# Resolving a merge conflict in the xrpl.js monorepo

The tree is the xrpl.js monorepo: npm workspaces under `packages/` (`xrpl`,
`ripple-binary-codec`, `ripple-keypairs`, `ripple-address-codec`, `@xrplf/isomorphic`,
`@xrplf/secret-numbers`), TypeScript throughout, jest unit tests beside the sources.

## Files you must not resolve by hand

- `packages/ripple-binary-codec/src/enums/definitions.json` and `package-lock.json` carry a
  `merge=ours` attribute and are rewritten after every merge. If either one still reaches you,
  restore the version already in the tree with `git checkout --ours -- <path>` and stage it.

## The rule for everything else

Keep both sides. The tree already contains earlier feature branches; this merge adds one more.
A conflict is almost always two branches adding different entries to the same list, and the
answer is the union in a stable order, not a choice between them.

## Per file

- `packages/*/src/models/transactions/index.ts`, `models/ledger/index.ts`,
  `models/methods/index.ts`: barrel files of `export ... from './X'` lines and union types.
  Keep every export from both sides, and list every member in the union type
  (`export type Transaction = ... | ContractCall | ...`). A missing member breaks the type, a
  duplicate export breaks the build.
- `packages/xrpl/src/models/transactions/transaction.ts`: the `validate` switch over
  `TransactionType`. Keep both sides' `case` blocks and both sides' imports.
- `packages/xrpl/src/models/transactions/common.ts`, `models/common/index.ts`: shared types
  and guards. Keep both sides' additions; do not renumber or rename existing members.
- `packages/ripple-binary-codec/src/types/index.ts` and `coreTypes`: keep both sides' type
  registrations. The names must match the `TYPES` keys the node reports, so never rename one.
- `packages/*/package.json`: keep both sides' dependencies; on a version conflict take the
  higher version. Never edit `package-lock.json` to match; it is regenerated.
- Test files under `packages/*/test/`: keep both sides' suites. Two `describe` blocks with the
  same name are fine; a dropped test is not.
- `HISTORY.md`: keep both sides' entries under the unreleased heading, base's first.

## Finishing

Remove every conflict marker, `git add -A`, and check `git diff --check` is silent. The result
must typecheck: imports resolve, every union member exists, no duplicate identifiers. Do not
commit and do not abort the merge.
