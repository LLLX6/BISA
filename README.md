# BISA | بيسا

> اكتشافاتك، قريبة منك.
> Your discoveries, close to you.

BISA is a bilingual, mobile-first local commerce marketplace for discovering selected products priced from **100 baisa to OMR 2** at nearby stores. It is a new, independent project with its own identity, data model, session namespace, PWA cache, storage paths and Git history.

بيسا سوق محلي ثنائي اللغة ومصمم للهاتف أولاً، لاكتشاف منتجات مختارة بين **100 بيسة و2 ر.ع** من متاجر قريبة. المشروع مستقل بهويته وبياناته وجلساته وذاكرة PWA وتاريخ Git الخاص به.

![BISA logo](assets/brand/bisa-logo.svg)

## Product rules | قواعد المنتج

- Every product is validated by the server and database to be between `0.100` and `2.000` OMR.
- A bundle or cart may exceed OMR 2; each component product may not.
- One active cart belongs to one store. Moving to another store requires explicit replacement confirmation.
- Checkout creates an idempotent order and inventory reservations. Stock is decremented only when the merchant accepts.
- Areas are public only when they contain an approved, active, public branch.
- Store, office and home fulfillment are merchant-provided. BISA does not claim to operate a delivery fleet.
- Payment, WhatsApp and Push never report success when unconfigured. The map provider is server-controlled; OpenStreetMap is the reviewed V1 default and can be disabled without code.
- Production sample data is disabled by default.

## Roles | الأدوار

- Guest and shopper
- Merchant owner, manager and staff
- Supplier advertiser (merchant-only Supplier Hub)
- Support admin, admin and super admin

Authorization is enforced in the Python domain layer. Hiding UI controls is not treated as authorization.

## Run locally | التشغيل المحلي

Requirements: Python 3.12+ and, for UI tests, Node.js 22+.

```powershell
# The runtime reads process environment variables directly; .env.example is a template.
$env:BISA_SEED_SAMPLE_DATA='true' # local demo only
$env:BISA_DEMO_PIN = Read-Host 'Choose a local 4-8 digit demo PIN'
python -m pip install -r requirements.txt
python bisa_server.py
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080).

Opt-in demo fixtures exist only when `BISA_SEED_SAMPLE_DATA=true` and require a locally chosen `BISA_DEMO_PIN`; there is no runtime default PIN in source. The sample catalog contains tagged stores, products, bundles and advertisements for isolated local testing. Do not reuse a demo database or PIN in production. An authorized administrator can remove tagged demo records from **BISA Admin → Demo data** with the exact confirmation phrase shown in the interface; real records are not selected by that operation.

The browser shell uses the same origin for its API by default. Running `bisa_server.py` therefore serves the UI and transactional API together locally. `.github/workflows/pages.yml` is only an owner-triggered, manually confirmed static preview path; this repository does not claim that preview is currently published. A static preview cannot provide real ordering unless a separately approved backend, CORS policy and API origin are configured.

Never enable sample seed against production storage.

## Quality gates | بوابات الجودة

```powershell
python -m pip install -r requirements.txt
$bisaPython = Get-ChildItem -File bisa_*.py,scripts/*.py,tests/test_bisa*.py | Select-Object -ExpandProperty FullName
python -m py_compile $bisaPython
python -m unittest discover -s tests -p "test_bisa*.py" -v
python scripts/verify_bisa.py
npm ci
npm audit --audit-level=high
npx playwright install chromium
npm run check:js
npm run test:map
npm run test:ui
npm run test:performance
git diff --check
```

## Administration

Provision the first administrator interactively; the PIN is never written to source control:

```powershell
python scripts/create_bisa_admin.py
```

Then open `/?view=admin` and sign in. Merchant approval activates the store, public branch, merchant role and 90-day early trial atomically, with an audit entry.

## Architecture

- `bisa_config.py` — central identity, paths, environment and price policy.
- `bisa_domain.py` — schema, validation, authorization and commerce transactions.
- `bisa_server.py` — bounded JSON API and strict static hosting surface.
- `assets/scripts/bisa-app.js` — shopper, merchant and administrator presentation.
- `assets/scripts/bisa-map.js` — bounded Leaflet branch map using server-approved provider configuration.
- `bisa_moderation.py`, `bisa_merchant_launch.py`, `bisa_push.py` — authorized review receipts, branch launch lifecycle and role-scoped Web Push outbox.
- `assets/styles/bisa.css` — responsive RTL/LTR design system.
- `public/` — verified mirrors for static hosting.
- `tests/` — price, plans, cart, order, authorization and responsive browser tests.
- `docs/` — product, architecture, security and operating decisions.

Detailed notes: [architecture](docs/ARCHITECTURE.md), [product](docs/PRODUCT.md), [security](SECURITY.md), [foundation release](release-notes/0.1.0-foundation.md).

## Production status

The repository is ready for review and local evaluation. It is **not publicly deployed by this repository**. Production additionally requires an explicit owner-approved stable release, durable BISA-only storage, real domain/CORS settings, tested backups and monitoring. Phone onboarding remains invite-only until a real verification flow exists. Payment and WhatsApp remain unavailable until real adapters are implemented; Push requires a valid VAPID credential set and a live-device acceptance test. OpenStreetMap is selected for the V1 branch map under its official tile policy, with visible attribution, no automatic GPS request and no offline tile prefetch; the owner can disable it from Admin settings.

Copyright © 2026 BISA. All rights reserved.
