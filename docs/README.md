# docs/

Four documents, two of them historical. Read them in this order if you want
the engineering narrative; none of them are needed to run or evaluate the
system — `/README.md` and `/REPORT.md` are.

| Document | Status | What it is |
|---|---|---|
| [`parabank-seeding.md`](parabank-seeding.md) | **current** | How the local ParaBank instance is seeded, and why the fixture personas are shaped the way they are. Useful if you are running the demo path. |
| [`implementation-review-and-plan.md`](implementation-review-and-plan.md) | **current** | The most recent review: 36 findings, a clause-by-clause audit against the assignment brief, what was built in response, and what was deliberately *not* built with reasons. This is the one to read. |
| [`review-findings.md`](review-findings.md) | historical | An earlier review pass. Superseded by the document above; kept because its findings are referenced by commit messages and by code comments explaining why something is the way it is. |
| [`remediation-plan.md`](remediation-plan.md) | historical | The work plan that came out of `review-findings.md`. Same reason for keeping it. |

The two historical documents describe a state of the codebase that no longer
exists. They are kept rather than deleted because several code comments cite
their finding numbers as the reason a particular decision was made, and a
citation pointing at a deleted file is worse than one pointing at an outdated
one. Where they disagree with the current code, the code is right.
