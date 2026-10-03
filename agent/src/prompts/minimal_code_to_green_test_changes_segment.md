Test changes this run, read from the repository itself (not from the test suite summary above,
which lists only what the tests stage said it wrote):

<<test_changes>>

New and updated tests are the ones your implementation must turn green. Deleted tests were removed
on purpose, together with the acceptance criterion they covered: do not recreate them, and do not
write new tests for that removed behaviour. Remove the production code that served only them through
the Plan steps that declare `removes_ids`; the remaining suite must stay green after the removal.
