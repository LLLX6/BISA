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
- Payment, WhatsApp, maps and Push adapters never report success when unconfigured.
- Production sample data is disabled by default.

## Roles | الأدوار

- Guest and shopper
- Merchant owner, manager and staff
- Supplier advertiser (merchant-only Supplier Hub)
- Support admin, admin and super admin

Authorization is enforced in the Python domain layer. Hiding UI controls is not treated as authorization.

## Run locally | التشغيل المحلي

Requirements: Python 3.12+ and, for UI tests, Node.js 20+.

```powershell
Copy-Item .env.example .env
$env:BISA_SEED_SAMPLE_DATA='true' # local demo only
python bisa_server.py
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080).

Local demo accounts exist only when `BISA_SEED_SAMPLE_DATA=true`:

- Shopper: `96890000001` / `1234`
- Merchant (Mawaleh Daily): `96892000003` / `1234`

The complete sample catalog contains 6 tagged demo stores across Muscat's wilayats, 24 products, 6 bundles and 6 advertisements. An authorized administrator can remove all tagged demo records from **BISA Admin → Demo data** by entering the exact confirmation phrase shown in the interface. Real records are not selected by that operation.

The public phone showcase is deployed from `public/` to GitHub Pages. It demonstrates discovery, area filters, a one-store cart and the admin preview without pretending to submit a real order; transactional ordering requires the Python server.

Never enable sample seed against production storage.

## Quality gates | بوابات الجودة

```powershell
python -m py_compile bisa_config.py bisa_domain.py bisa_server.py scripts/create_bisa_admin.py
python -m unittest tests.test_bisa_domain -v
npm ci
npm audit --audit-level=high
npm run check:js
npm run test:ui
npm run test:performance
python scripts/verify_bisa.py
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
- `assets/styles/bisa.css` — responsive RTL/LTR design system.
- `public/` — verified mirrors for static hosting.
- `tests/` — price, plans, cart, order, authorization and responsive browser tests.
- `docs/` — product, architecture, security and operating decisions.

Detailed notes: [architecture](docs/ARCHITECTURE.md), [product](docs/PRODUCT.md), [security](SECURITY.md), [foundation release](release-notes/0.1.0-foundation.md).

## Production status

The repository is ready for review and local/staging evaluation. It is **not publicly deployed by this repository**. Production requires explicit owner approval plus persistent storage, real domain/CORS settings, backups, monitoring, and any selected payment/WhatsApp/maps/Push vendors.

Copyright © 2026 BISA. All rights reserved.
