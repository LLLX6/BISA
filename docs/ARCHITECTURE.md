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

Payments and WhatsApp stay disabled until real adapters are implemented. Web Push stays unavailable until a valid VAPID pair is configured and verified. The V1 branch map uses the locally bundled Leaflet runtime and the server-approved OpenStreetMap tile template; attribution and a map-issue link stay visible, GPS is opt-in, and tiles are neither bulk-downloaded nor cached for offline use. Every integration keeps an explicit unavailable state and never returns synthetic success. BISA V1 delivery is fulfilled by the merchant, not by a BISA fleet.

Map implementation references: [OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/) and [Leaflet 1.9.4 reference](https://leafletjs.com/reference.html). The bundled Leaflet license and third-party notice are committed with the source.

## Production decisions remaining

- Select persistent database/storage hosting and backup target.
- Decide whether to retain SQLite or implement and test a PostgreSQL adapter.
- Select payment and WhatsApp vendors; approve production VAPID custody and live Push acceptance testing; periodically review the OpenStreetMap tile policy or replace the provider through the server setting.
- Perform Omani legal/privacy review and penetration testing.
- Establish monitoring, incident response and recovery objectives.

## Security and operations boundary

Migration `003_security_operations.sql` adds only new columns, tables and
indexes. `bisa_security.py` is deliberately independent from the commerce
domain and supplies persisted login throttling, short access sessions, rotating
refresh sessions, exact-session logout, role revalidation, database-owned
permissions, and signed private-media access.

Required HTTP integration:

1. Before checking a PIN, call `ensure_login_allowed(phone, source_id=...)`.
   On an invalid credential call `record_login_failure`; on success call
   `clear_login_failures`. `guarded_credentials` combines these steps around a
   credential callback. Account and source buckets use separate thresholds so
   carrier-grade NAT cannot be locked by the much lower per-account limit.
2. After credential verification, call `issue_session(account_id, role,
   merchant_id, device_id)`, then `session_http_exchange`. Return only its JSON
   payload and emit its second value as `Set-Cookie`. The refresh token is then
   held in a role-scoped `Secure; HttpOnly; SameSite=Strict` BISA cookie and is
   never put in browser JavaScript storage.
3. Replace request authentication with `authenticate_access`. It rechecks the
   account, exact active `account_roles` row, and approved/active merchant on
   every request. Manager/staff sessions also require a matching active
   `merchant_members` record; supplier sessions require an approved supplier
   and active `supplier_members` record.
4. `POST /api/auth/refresh` calls `rotate_refresh_token`; refresh-token reuse
   revokes the complete session family. `POST /api/auth/logout` calls
   `logout_session` with the current access token only. It must not revoke a
   second device or another role accidentally. Read the selected role token
   with `refresh_token_from_cookie`, rotate it, replace its cookie, and clear
   only that role with `clear_refresh_cookie_header` on logout.
5. Resolve authorization with `require_permission(actor, permission,
   merchant_id=...)`; never accept role or merchant scope from request JSON.

Merchant and support documents must be stored below the BISA-only
`BISA_UPLOAD_DIR/private/` directory, registered with `register_private_media`,
and referenced externally only by opaque media ID. The metadata response never
contains `storage_key`. The authenticated download endpoint obtains a short
route from `signed_private_media_route`, then calls
`resolve_private_media_path`; it must stream with `Cache-Control: private,
no-store` and must never expose the private directory through static hosting.
Merchant staff do not inherit access to CR/licence documents merely by sharing
the merchant ID. Private media creation and grant delegation require ownership
or `private_media.manage`; read access alone cannot escalate into delegation.

`bisa_integrations.py` defines dependency-injected WhatsApp, payment, email and
Push adapters. The repository defaults are unavailable adapters. Credentials
alone never turn an operation into success: a reviewed concrete adapter must
return a provider reference, and `execute_external_action` records only status
metadata—not message bodies, addresses, documents, tokens or payment secrets.

`bisa_jobs.py` contains scheduler-safe one-shot functions. A platform scheduler
may invoke `run_operations_once`; the module never launches a daemon. Order
expiry is conditional and idempotent and releases pending reservations.
Inventory freshness is marked without hiding/deleting products. The checkout
path should populate `orders.expires_at`, and a successful stock confirmation
must reset `freshness_status='fresh'`, `stale_at=''` and
`stale_enforcement=''` inside the same transaction.

At process readiness, merge `security_production_readiness()` into the existing
readiness response. Production must remain not-ready until both
`BISA_AUTH_PEPPER` and `BISA_MEDIA_SIGNING_KEY` contain at least 32 characters
and are distinct. The readiness result exposes only booleans/error names, never
secret values.
