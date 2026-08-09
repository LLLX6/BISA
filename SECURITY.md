# BISA security policy

## Supported version

The current foundation line is `0.1.x`. Security reports should not include real customer, merchant or credential data.

## Controls in the foundation

- PBKDF2-HMAC-SHA256 PIN hashes with a per-account random salt.
- Random bearer sessions stored only as SHA-256 token hashes.
- Server authorization for shopper, merchant and administrator boundaries.
- Parameterized SQL, foreign keys, WAL transactions and bounded JSON/input sizes.
- Database price checks and partial uniqueness for one active cart.
- Idempotent checkout and guarded order transitions.
- Non-public merchant applications and area/branch approval gates.
- CSP, no-store API responses, origin allowlist and no sensitive lock-screen payload.
- No production seeds and no secret-bearing configuration committed.

## Reporting

Report privately to the repository owner with reproduction steps and impact. Do not open a public issue containing credentials, personal data or a live exploit.

## Before production

An independent penetration test, dependency review, data protection/legal review, production backup drill and external adapter security review are required.
