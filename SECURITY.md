# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** through GitHub:
[Security → Report a vulnerability](https://github.com/niravpatidar37/hopcite/security/advisories/new).
Do not open a public issue for a security problem.

Especially valuable reports:

- A question or document that makes the model's output reach Neo4j as Cypher (the router must
  only ever select a constant template; see `tests/test_no_model_cypher.py`).
- An answer whose citation is not one of the chunk ids the server retrieved for that question.
- A way past the API key, input limits, rate limit, kill switch or spend cap.
- Secrets or personal data showing up in logs, images or the repository.

Please include steps to reproduce and the commit you tested. I aim to acknowledge reports
within 7 days. This is a portfolio project with no SLA and no bug bounty.

## Scope and known limits

The residual risks are listed in the README ([Security model](README.md#security-model)), for
example prompt injection that steers the wording of an answer while citing a real chunk, or a
poisoned document that is ingested like any other. Reports that improve on those are welcome too.

## Supported versions

Only `main` is supported.
