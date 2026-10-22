# Security policy

This is a portfolio of self-contained demonstration systems. Nothing here runs as a hosted
service, holds real money, or processes real personal data: every project generates
deterministic synthetic data in code, needs no API keys, and makes no network calls. The
only deployed artifact is the static dashboard on GitHub Pages.

That said, the code is written to production standards, and reports are welcome.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting:
**Security → Report a vulnerability** on this repository
(or <https://github.com/ryanwien/FintechPortfolio/security/advisories/new>).

Alternatively, reach me via [LinkedIn](https://www.linkedin.com/in/ryanwien3d).

Please do not open a public issue for anything you believe is exploitable.

## What counts

Reports I'd consider meaningful here:

- A way to make the ledger violate its invariants (lose or create money, double-apply an
  idempotent request, bypass the overdraft guard); these are the product guarantees.
- SQL injection or similar in the ledger's persistence layer.
- XSS on the dashboard (note: project run reports are rendered from this repo's own
  generated files, not user input, but a hole in that path still matters).
- A dependency pinned in any `requirements.txt` with a known CVE that is actually
  reachable from this code.

Out of scope: the models' statistical performance (documented limitations, not
vulnerabilities), denial-of-service against your own local run, and anything requiring
write access to the repository.

## Supported versions

Only `main` as deployed. There are no releases or backports.
