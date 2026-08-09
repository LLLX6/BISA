# BISA engineering rules

These rules apply to the entire BISA repository.

## Independence and safety

- BISA is independent. Never use or modify another product's databases, uploads, backups, sessions, cookies, caches, deployments, secrets, remotes or production accounts.
- Never commit `.env`, databases, uploads, backups, logs, PINs, tokens, personal data or sample production records.
- Do not deploy publicly or change DNS, paid plans or external accounts without explicit owner approval.
- Keep production seed data disabled.

## Product invariants

- Enforce each product price between 100 and 2,000 baisa on the server and in the database.
- Bundles may exceed OMR 2 only when every component product is valid.
- Keep one active store per cart and require explicit confirmation to replace it.
- Reserve inventory idempotently; decrement it only on merchant acceptance.
- Do not expose an area, merchant, branch, product, advertisement or supplier campaign before its approval rules pass.
- Do not claim payment, WhatsApp, Push, maps or delivery succeeded when its adapter is unavailable.

## Engineering and verification

- Private multi-user state belongs to the server/database; UI hiding is never authorization.
- Use parameterized SQL and bounded input.
- Maintain Arabic RTL, English LTR and 320/375/390/430px layouts.
- Keep root/public HTML, manifest, service worker and BISA assets byte-identical.
- Review `git status` and `git diff` before every commit.
- Run Python compile, BISA unit tests, JS checks, UI smoke, performance size and `scripts/verify_bisa.py`.
- Record commands, results, external actions and known limitations in `release-notes/`.
