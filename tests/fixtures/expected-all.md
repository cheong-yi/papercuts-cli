# Papercuts

## 00000000-0000-4000-8000-000000000101 | resolved | docs | recurrence=2
    summary="summary <b>x</b> [link](https://example.test) <https://example.test> `tick` \\ | pipe\nnext"
    expected="expected <i>x</i> [link](https://example.test) <https://example.test> `tick` \\ | pipe\nnext"
    observed="observed <u>x</u> [link](https://example.test) <https://example.test> `tick` \\ | pipe\nnext"
    evidence_basis=test
    resolution="resolution <em>x</em> [link](https://example.test) <https://example.test> `tick` \\ | pipe\nnext"

## 00000000-0000-4000-8000-000000000102 | open | tooling | recurrence=2
    summary="second"
    expected="works"
    observed="broken"
    evidence_basis=inferred

## 00000000-0000-4000-8000-000000000103 | open | validation | recurrence=1
    summary="third"
    expected="a result"
    observed="no result"
    evidence_basis=user_observation
