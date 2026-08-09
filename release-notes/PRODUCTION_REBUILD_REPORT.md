# BISA Production Rebuild Report | تقرير إعادة بناء بيسا

> **الحالة النهائية لهذه اللقطة:** بناء تطوير محلي مكتمل وقابل للمراجعة، وليس إصدار إنتاج منشوراً.
> **التاريخ:** 2026-08-10 — Asia/Muscat
> **النطاق:** مستودع `LLLX6/BISA` فقط. لا يشمل أي تعديل على Khadamati.
> **قرار نشر المصدر والمعاينة:** `OWNER APPROVED` بتاريخ 2026-08-10؛ يُسمح برفع هذا الفرع وفتح Draft PR ونشر معاينة صريحة.
> **قرار الإنتاج التجاري:** `BLOCKED BY EXTERNAL READINESS` — لا يُسمى الإصدار Production ولا يُدمج إلى `main` قبل إغلاق الحواجز الخارجية وقطع نسخة Stable مستقلة.

## A. تقرير السلامة | Safety report

### BISA

| البند | القيمة |
|---|---|
| المسار المحلي | `<BISA_REPO>` |
| المستودع | `https://github.com/LLLX6/BISA.git` |
| الفرع | `codex/bisa-production-rebuild` |
| نقطة البداية | `2e48c43e5dab9d18f2af16cb6f969734a348e4eb` |
| قاعدة البيانات | `data-bisa/bisa.sqlite3` |
| App ID | `om.bisa.marketplace` |
| Namespace | `BISA_*`, `bisa.*`, `bisa-pwa-*` |
| Sample seed | معطل افتراضياً؛ يُفعّل محلياً فقط |

### Khadamati الأصلي

ثبت الفحص القرائي أن المصدر المتتبع في Khadamati بقي دون تغيير:

| البند | القيمة المثبتة |
|---|---|
| الفرع | `release/production-readiness` |
| HEAD | `eeb8191b3577856775ecb5f8db5a69ba63238886` |
| Tree | `b4f223ea6023e88ac5a2f54be06ff8028027e903` |
| Remote | `https://github.com/LLLX6/Khadamati.git` |
| Tracked diff | `0` |

توجد ملفات PDF/DOCX ومخرجات غير متتبعة داخل `output/` و`tmp/` في Khadamati تغيّرت بواسطة مهمة أخرى متزامنة. لم تُمس ضمن BISA، لذلك الصياغة الدقيقة هي: **مصدر Khadamati المتتبع لم يتغير**؛ أما بصمة الملفات غير المتتبعة فليست جزءاً من هذا الضمان.

## B. ما تم بناؤه | Implemented product

BISA لم تعد إعادة تسمية لخدماتي. أصبحت منصة تجارة محلية مستقلة بهوية ونطاق بيانات ومسارات عمل جديدة:

- اكتشاف متاجر وفروع ومنتجات حسب الولاية والمنطقة، مع بحث وفلاتر سعر وتوفر وتسليم وترتيب بالمسافة عند طلب الموقع صراحة.
- Home مكوّن من 13 وحدة Server-driven: Hero، شرائح السعر، الأقسام، وصل اليوم، لقطات تستاهل، قريب منك، الباقات، المكتب، المنزل، المجاني، متاجر المنطقة، المتاجر الجديدة، والحملات.
- صفحة متجر وفرع، تبديل فروع السلاسل، ملخص رسوم وحدود التوصيل، سياسة الاسترجاع، كتالوج وباقات.
- صفحة منتج بصور ثابتة 1:1، سعر وتوفر وفرع وحفظ ومشاركة واتجاهات.
- سلة متجر واحد مع حوار صريح قبل استبدال المتجر، تعديل كمية وحذف، ثم مراجعة Checkout.
- طلبات Idempotent، حجز مخزون ذري، إعادة تسعير، Snapshot للسياسة والتسليم، دورة حالة، إلغاء/انتهاء يعيدان المخزون بأمان.
- رحلة تاجر: Wizard من 13 خطوة، وثائق خاصة، فروع، ساعات، Fulfillment، سياسة، خطط، منتجات، باقات، مخزون، طلبات، حملات وفريق.
- دورة إطلاق فرع: Draft خاص → تجهيز ساعات/موقع/وثيقة → Submit → مراجعة إدارة → Approve/Changes/Reject → نشر عام؛ مع Pause/Resume وتدقيق وإشعار.
- هوية متجر آمنة: Logo/Cover مرفوعان كوسائط خاصة ثم يرتبطان كأصول عامة Opaque بعد الاعتماد فقط.
- Supplier Advertiser مستقل: حساب مورد مفوض، مسودات وحملات وصور خاصة وLeads، وطابور مراجعة كامل قبل النشر للتجار.
- لوحة إدارة ذات صلاحيات دقيقة: طلبات، متاجر وفروع، مراجعة منتجات وإعلانات بإيصالات مراجعة قصيرة العمر، مواقع، أقسام، خطط، إعدادات، موردون وحملاتهم، تدقيق وأمان.
- خريطة فروع حقيقية بـLeaflet محلي وOpenStreetMap: علامات Keyboard/ARIA، Attribution ورابط بلاغ ظاهر، لا GPS تلقائي، لا Offline tile prefetch، ومفتاح تعطيل من Admin.
- حدود مسقط موحدة Server-side مع عقد الخريطة؛ لا يمكن تسجيل أو اعتماد فرع بدبوس خارج نطاق الإطلاق ثم اختفاؤه من الخريطة.
- Web Push حقيقي اختياري: ربط حسب الدور، نفس المتصفح لعدة أدوار دون تسريب، Outbox معاملاتي، claim tokens/retry/expiry/dedupe، Payload يحوي `notificationId` فقط، تحقق Vendor/DNS ضد SSRF، ولا إذن عند التحميل.
- PWA مستقلة: Manifest وأيقونات Any/Maskable وService Worker وCache وLocal Storage/Cookie namespaces خاصة بـBISA.
- العربية RTL والإنجليزية LTR، أهداف لمس 44px، Desktop workspace، حالات تحميل/فراغ/خطأ، وFocus trap للحوار.

## C. ملفات ووحدات المصدر | Source modules

### Backend

- `bisa_config.py` — الهوية المركزية، الإصدار، المسارات، السعر، CORS وProduction readiness.
- `bisa_domain.py` — المخطط الأساسي، قواعد 100–2000 بيسة، Seed محلي موسوم.
- `bisa_migrations.py` — Runner مرتب مع Checksum وRollback للمigration الفاشل.
- `bisa_security.py` — Access/Refresh rotation، Lockout، RBAC، جلسات Scoped، وسائط خاصة موقعة.
- `bisa_marketplace.py` — الاكتشاف، المتجر/المنتج، السلة، الطلبات، المخزون، الخطط، الإدارة والتحليلات.
- `bisa_operations.py` — Onboarding، العناوين، Cart mutations، منتجات/فروع/سياسات/حملات.
- `bisa_supplier.py` — Supplier advertiser والحملات والـLeads.
- `bisa_moderation.py` — مراجعة منتج/إعلان كاملة، Media resolver، Receipt one-time actor-bound.
- `bisa_merchant_launch.py` — دورة اعتماد الفروع وربط أصول العلامة.
- `bisa_push.py` — Push bindings/outbox/worker وVAPID transport الآمن.
- `bisa_integrations.py`, `bisa_jobs.py`, `bisa_application.py`, `bisa_server.py` — حدود الموصلات، jobs، composition وHTTP.

### Frontend/PWA

- `index.html` + `public/index.html` — App shell وSVG icon system.
- `assets/scripts/bisa-app.js` — Shopper/Merchant/Supplier/Admin journeys.
- `assets/scripts/bisa-map.js` — الخريطة المعزولة.
- `assets/styles/bisa.css` — Design system Mobile-first ثنائي الاتجاه.
- `vendor/leaflet.*`, `THIRD_PARTY_NOTICES.md` — Runtime محلي وترخيص موثق.
- `service-worker.js`, `manifest.webmanifest`, الأيقونات والصور ومرايا `public/`.

## D. قاعدة البيانات والترحيلات | Database and migrations

Runtime الحالي SQLite مستقل عن Khadamati. الترحيلات Additive ومتحققة بالـChecksum:

1. `002_marketplace_core.sql` — أساس التجارة، snapshots، cart/order، fulfillment، bundles/plans/ads/suppliers.
2. `003_security_operations.sql` — الجلسات والأدوار والصلاحيات والوسائط الخاصة وحقول التشغيل.
3. `004_marketplace_invariants.sql` — سلامة tenant/branch، inventory ledger/reservations/idempotency/status invariants.
4. `005_supplier_notification_isolation.sql` — عزل إشعارات المورد عن حساب المتسوق متعدد الأدوار.
5. `006_moderation_review_receipts.sql` — Receipts قصيرة العمر، مرتبطة بالمراجع والحالة وتُستهلك مرة واحدة.
6. `007_role_scoped_push_outbox.sql` — Push bindings حسب الدور وOutbox معاملاتي مع claim leases.

أدوات التشغيل: Backup ذري مع manifest/checksum، Restore إلى هدف فارغ فقط، Database audit، Production preflight وRepository verifier.

## E. الأمن والخصوصية | Security verification

- صلاحيات وPlan limits وقاعدة السعر وSupplier Hub والتحولات كلها Server-side.
- جلسات Access قصيرة وRefresh داخل Cookie `HttpOnly`, `Secure` في production, `SameSite=Strict`, role-scoped، مع rotation/reuse detection.
- تعطيل الدور/التاجر يبطل الجلسة؛ Logout لا يلغي أدوار الحساب الأخرى.
- وسائط CR/licence والعنوان والهاتف لا تصبح Public URLs ولا تظهر في Bootstrap العام.
- IDOR مغطى للمنتجات والفروع والطلبات والمستندات والوسائط والإشعارات والموردين.
- الاعتماد الأعمى ممنوع: المنتج/الإعلان يحتاج مراجعة مفوضة وReceipt صالح غير stale وغير مستهلك.
- الإعلان غير المدفوع أو الذي لا يملك رصيداً مشمولاً لا يمكن اعتماده أو نشره.
- Push endpoint محصور في HTTPS hosts المعروفة مع DNS revalidation ومنع private/loopback/link-local/reserved/redirects.
- لا تُرسل مستندات أو بيانات خاصة في Push؛ Payload يحوي معرف إشعار فقط والنص العام يُجلب بعد التفويض.
- CSP/HSTS/nosniff/referrer policy ومدخلات bounded وParameterized SQL.
- لا أسرار أو `.env` أو DB أو uploads أو logs أو بيانات شخصية ضمن Source archive.

## F. نتائج الاختبارات | Verification results

> تُحدّث هذه الخانة من آخر تشغيل بعد توقف التعديلات وقبل Commit. لا تُعد أرقاماً تسويقية.

| البوابة | النتيجة النهائية |
|---|---|
| Python compile | PASS |
| Full `test_bisa*.py` | 140/140 PASS — 281.551s |
| HTTP API focused | 13/13 PASS |
| Moderation / branch launch / Push / migrations | 7/7 + 10/10 + 9/9 + 4/4 PASS |
| Map unit | 4/4 PASS |
| UI full | PASS: 320, 375, 390, 430, 1280؛ RTL/LTR؛ Shopper/Merchant/Supplier/Admin/Push/Map |
| Performance budgets | PASS: core shell 349,537/380,000 B؛ map bundle 175,770/180,000 B؛ hero 62,216/100,000 B |
| Repository verifier | يُشغّل بعد Stage الصريح لأن البوابة ترفض أي ملف إصدار غير متتبع |
| `npm audit --audit-level=high` | PASS — 0 vulnerabilities |
| `git diff --check` | PASS |

ملخص UI المثبت: 13 Home sections، لا Horizontal overflow، Touch targets صحيحة، List/Map وفروع فعلية، Shopper cart/checkout/order/address، Merchant catalog/stock/branches/fulfillment/policy/team، Supplier draft/private creative/review، Admin full moderation، وPush 8 عقود.

## G. التشغيل المحلي | Exact local run

```powershell
cd "<BISA_REPO>"
python -m pip install -r requirements.txt
$env:BISA_ENV='development'
$env:BISA_SEED_SAMPLE_DATA='true'
$env:BISA_DEMO_PIN = Read-Host 'Choose a local 4-8 digit demo PIN'
$env:BISA_PHONE_VERIFICATION_MODE='development_bypass'
python bisa_server.py
```

ثم افتح `http://127.0.0.1:8080`.

بيانات Demo محلية فقط. لا توجد PIN افتراضية في كود التشغيل؛ يلزم إدخال `BISA_DEMO_PIN` محلياً، ولا يجوز تشغيل Seed ضد تخزين إنتاج.

## H. التكاملات الخارجية المطلوبة | External credentials/actions

- Production VAPID public/private/subject + اختبار Push على iOS/Android حقيقيين.
- PSP إنتاجي وتدفق Activate/Refund/Reconcile؛ التطبيق لا يدعي دفعاً ناجحاً الآن.
- WhatsApp Business/Cloud API إن تقرر استخدامه؛ الإشعار الداخلي يعمل بدونه.
- موصل بريد ومراقبة/Sentry أو بديل مع سياسة احتفاظ معتمدة.
- Domain/CORS وTLS وSecrets manager وخطة تخزين دائم ونسخ احتياطي خارج الجهاز.
- مراجعة قانونية عمانية للخصوصية، المستهلك، الإعلانات، سياسات المتاجر والموردين.

## I. حواجز الإطلاق والقرارات المتبقية | Launch blockers

1. الإصدار `0.2.0-dev`؛ يجب Cut ذري إلى SemVer stable يطابق API/package/lock/manifest/SW/cache.
2. Render blueprint مجاني وغير دائم؛ يلزم تخزين BISA دائم وخطة Backup/Restore مجرّبة قبل الإنتاج.
3. Phone registration يبقى `invite_only` في production إلى حين OTP/identity verification حقيقي ومقاوم للإساءة.
4. اشتراكات Basic/Advanced تنتهي `pending_payment` بأمان؛ لا يوجد PSP activation بعد، فلا إطلاق تجاري مدفوع.
5. الإعلانات تبقى غير قابلة للاعتماد قبل دفع/رصيد حقيقي؛ يلزم Purchase/Credit/Reconciliation flow للإيراد التجاري.
6. Payment وWhatsApp والبريد غير منفذة كموصلات إنتاجية؛ واجهاتها صادقة وغير متاحة.
7. Push يحتاج مفاتيح حقيقية واختبار Vendor/live device؛ لا توجد مفاتيح في المستودع.
8. الخريطة تستخدم OpenStreetMap وفق سياستها الحالية؛ يلزم قبول مالك مستمر للسياسة والخصوصية أو تغيير المزود من Admin.
9. اختبارات Accessibility الآلية والرحلات مغطاة، لكن يلزم فحص VoiceOver/TalkBack/NVDA وأمن خارجي قبل الإطلاق العام.
10. GitHub Pages static preview ليس تجربة Transactional؛ النشر الآمن المقترح Same-origin من خادم BISA أو Proxy معتمد.

## J. حالة Git والإطلاق | Git and deployment state

- Branch: `codex/bisa-production-rebuild`.
- لا دمج إلى `main`.
- رفع الفرع وفتح Draft PR ونشر معاينة صريحة مسموح بها بتفويض المالك الأخير.
- لا دمج إلى `main` ولا Stable Release ولا نشر تجاري مدفوع قبل إغلاق حواجز القسم I.
- Commit واحد من Stage allowlist الصريح، بعد فحص الأسرار والاختبارات النهائية.
- SHA النهائي يُسلّم خارج هذا الملف بعد إنشاء Commit؛ لا يمكن تضمين SHA الخاص بالCommit داخل محتواه دون حلقة تغيير ذاتي.

## K. قرار التسليم | Handoff decision

النتيجة هي **Development Release Candidate محلي قوي** وليس Production launch. يمكن تشغيله وعرضه محلياً ومراجعته كمنتج متكامل، لكن يجب إبقاء النشر محجوباً حتى يوافق المالك صراحة ويُغلق ما في القسم I. هذا التقرير لا يمنح موافقة نشر ولا يغيّر أي نظام خارجي.
