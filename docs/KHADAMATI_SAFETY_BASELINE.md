# BISA rebuild — Khadamati safety baseline

Recorded before any BISA rebuild code change on 2026-08-09 (Asia/Muscat).

## Original repository boundary

- Repository: `LLLX6/Khadamati`
- Local path: `<KHADAMATI_REPO>`
- Branch: `release/production-readiness`
- HEAD: `eeb8191b3577856775ecb5f8db5a69ba63238886`
- HEAD tree: `b4f223ea6023e88ac5a2f54be06ff8028027e903`
- Remote: `https://github.com/LLLX6/Khadamati.git`
- Tracked working-tree differences before baseline: `0`
- Existing untracked roots: `output/`, `tmp/`
- Existing untracked file count: `343`
- Existing untracked content fingerprint (path + size + SHA-256): `d31f6575b7979a4466bdbab1c190cef5cbd4ad4d309ec6fafeb086fb4092ab91`

The same HEAD, tree, zero tracked differences, untracked count and fingerprint were observed after the baseline suite. No Khadamati file, branch, tag, remote, database, upload, deployment or release was changed.

## Baseline results

| Gate | Result |
|---|---|
| Python source compilation | PASS |
| Unit tests | PASS — 113/113 |
| Security API | PASS — 12 controls |
| Trust API | PASS |
| Platform API | PASS |
| Isolated API smoke | PASS — 30 checks |
| JavaScript syntax | PASS |
| UI smoke | PASS — user, request, provider, admin and mobile fit |
| Local UI performance | PASS — DCL 65 ms, load 73 ms, FCP 108 ms, interactive 1050 ms |
| Repository verifier | BASELINE FAIL — pre-existing tracked `tmp/pdfs/kh-study.sqlite3` is prohibited |

The repository-verifier failure existed before this rebuild and was not corrected because the governing requirement is to leave the original repository unchanged.

## BISA isolation

- Independent repository: `LLLX6/BISA`
- Independent remote: `https://github.com/LLLX6/BISA.git`
- Rebuild branch: `codex/bisa-production-rebuild`
- BISA namespaces remain `BISA_*`, `bisa.*`, `om.bisa.marketplace`, `data-bisa/` and `bisa-pwa-*`.
- Production sample seed remains disabled.
- No production deployment is authorized for this rebuild.
