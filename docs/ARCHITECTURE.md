# BISA architecture | بنية بيسا

## Boundary

BISA is an independent local-commerce system. Its root of trust is the API and SQLite database. The PWA is a presentation client and never decides permissions, prices, plan limits, inventory or order transitions.

بيسا نظام تجارة محلية مستقل. الخادم وقاعدة البيانات هما مصدر الحقيقة؛ الواجهة لا تقرر الصلاحيات أو الأسعار أو حدود الباقات أو المخزون أو انتقالات الطلب.

## Runtime

```text
Browser / PWA
  ├─ Public catalog, search and visible locations
  ├─ Shopper: cart, checkout, orders and merchant application
  ├─ Merchant: today, orders, catalog, promotions and settings
  └─ Admin: merchant review and platform overview
             │ HTTPS JSON + bearer session
             ▼
bisa_server.py
             │ validated method calls
             ▼
bisa_domain.py
  ├─ authorization and bounded input
  ├─ product / plan / bundle policies
  ├─ idempotent checkout and inventory reservations
  ├─ merchant acceptance and atomic stock decrement
  ├─ approval workflows and audit logs
  └─ SQLite transaction layer (WAL + foreign keys)
```

## Namespaces

- Application ID: `om.bisa.marketplace`
- Local storage: `bisa.*`
- Session cookie reservation: `bisa_session`
- PWA cache: `bisa-pwa-*`
- Environment: `BISA_*`
- Database: `data-bisa/bisa.sqlite3`
- Uploads/backups: BISA-only paths

No runtime path or identifier points to another product.

## Data model

Migration `001` is embedded in the additive `SCHEMA` block in `bisa_domain.py` and recorded in `schema_migrations`. Main domains:

- identity: accounts, roles, sessions;
- geography: governorates, wilayats and public areas;
- merchants: applications, documents, stores, branches, fulfillment and policies;
- catalog: categories, products, branch inventory and bundles;
- commerce: carts, cart items, orders, item snapshots and reservations;
- business: plans, subscriptions, inventory audits, ads, suppliers and campaigns;
- platform: notifications, analytics, settings and admin audit records.

Money is stored as integer baisa. User-facing OMR is formatted at the boundary.

## Transaction boundaries

- Checkout: replay check → active cart → fulfillment validation → price/policy snapshot → order → component reservations → cart close → merchant notification.
- Accept order: lock → stage check → validate every reservation → decrement all components → consume reservations → transition order → resolve prompt → shopper notification.
- Approve merchant: application → merchant → branch visibility → merchant role → trial subscription → owner notification → admin audit.

## External adapters

Payments, WhatsApp, Push and live maps are disabled until configured and tested. Their UI uses explicit unavailable states; no synthetic success is generated. BISA V1 delivery is fulfilled by the merchant, not by a BISA fleet.

## Production decisions remaining

- Select persistent database/storage hosting and backup target.
- Decide whether to retain SQLite or implement and test a PostgreSQL adapter.
- Select payment, WhatsApp, Push and map vendors.
- Perform Omani legal/privacy review and penetration testing.
- Establish monitoring, incident response and recovery objectives.
