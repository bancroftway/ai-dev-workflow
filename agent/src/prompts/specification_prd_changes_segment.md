This round's PRD merge (a separate pass over the same requirements text) changed these existing
requirements. Each `PC-n` must be accounted for: cite it in the `prd_change_ids` of the
`story_decisions` row it modifies or retires, or -- only when no story is affected (say, the
specification never had that requirement) -- list it in `prd_changes_without_story` with a reason.
"implied" means the requirements text never named it: a new requirement contradicts or replaces it,
which is exactly the kind of change that is easy to miss.

<<changes>>

The PRD diff for this round (previous PRD -> this round):

```diff
<<diff>>
```
