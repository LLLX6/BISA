'use strict';

const {chromium} = require('playwright');
const {spawn, spawnSync} = require('child_process');
const {createServer} = require('net');
const {pbkdf2Sync} = require('crypto');
const path = require('path');
const fs = require('fs');
const os = require('os');

const root = path.resolve(__dirname, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'bisa-ui-'));
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const viewports = [
  {width: 320, height: 760},
  {width: 375, height: 812},
  {width: 390, height: 844},
  {width: 430, height: 932},
  {width: 1280, height: 800},
];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function centeredClick(locator) {
  await locator.evaluate(node => node.scrollIntoView({block: 'center', inline: 'center'}));
  await locator.click();
}

async function freePort() {
  return new Promise((resolve, reject) => {
    const socket = createServer();
    socket.once('error', reject);
    socket.listen(0, '127.0.0.1', () => {
      const port = socket.address().port;
      socket.close(error => error ? reject(error) : resolve(port));
    });
  });
}

async function waitForServer(base, getDiagnostics) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    try {
      const response = await fetch(`${base}/healthz`);
      if (response.ok) return;
    } catch {}
    await sleep(250);
  }
  throw new Error(`server_not_ready\n${getDiagnostics()}`);
}

function pageDiagnostics(page, consoleErrors, failedRequests) {
  return async label => {
    const snapshot = await page.evaluate(() => ({
      url: location.href,
      title: document.title,
      text: document.body.innerText.slice(0, 1200),
      overlayHidden: document.querySelector('#overlayRoot')?.hidden,
    })).catch(() => ({}));
    throw new Error(`${label}:${JSON.stringify({snapshot, consoleErrors, failedRequests})}`);
  };
}

function observePage(page) {
  const consoleErrors = [];
  const failedRequests = [];
  const httpErrors = [];
  const transparentPng = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
  page.route('https://tile.openstreetmap.org/**', route => route.fulfill({status: 200, contentType: 'image/png', body: transparentPng}));
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('requestfailed', request => failedRequests.push(`${request.method()} ${request.url()} ${request.failure()?.errorText || ''}`));
  page.on('response', response => {if (response.status() >= 400) httpErrors.push(`${response.request().method()} ${response.status()} ${response.url()}`);});
  return {consoleErrors, failedRequests, httpErrors, fail: pageDiagnostics(page, consoleErrors, [...failedRequests, ...httpErrors])};
}

async function assertHome(page, viewport, observations) {
  await page.goto(page.url().split('?')[0] || page.url(), {waitUntil: 'domcontentloaded'});
  await page.locator('[data-home-section="hero"] .hero-slide').waitFor({state: 'visible'});
  const report = await page.evaluate(() => {
    const html = document.documentElement;
    const hero = document.querySelector('.hero-slide');
    const bottomNav = document.querySelector('#shopperNav');
    const desktopNav = document.querySelector('#desktopNav');
    const interactive = [...document.querySelectorAll('button,a[href],input:not([type="hidden"]),select,textarea')]
      .filter(node => {
        const style = getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
      })
      .map(node => {
        const rect = node.getBoundingClientRect();
        const type = node.getAttribute('type') || '';
        const label = (node.getAttribute('aria-label') || node.textContent || node.getAttribute('name') || node.tagName).trim().slice(0, 80);
        const target = ['checkbox', 'radio'].includes(type) ? node.closest('label')?.getBoundingClientRect() : rect;
        return {tag: node.tagName, type, label, width: target?.width || rect.width, height: target?.height || rect.height};
      });
    return {
      sections: [...document.querySelectorAll('[data-home-section]')].map(node => node.dataset.homeSection),
      scrollWidth: html.scrollWidth,
      clientWidth: html.clientWidth,
      dir: html.dir,
      lang: html.lang,
      heroBackground: hero ? getComputedStyle(hero).backgroundImage : '',
      products: document.querySelectorAll('.product-card').length,
      stores: document.querySelectorAll('.store-card').length,
      bundles: document.querySelectorAll('.bundle-card').length,
      tooSmall: interactive.filter(item => item.width < 43.5 || item.height < 43.5),
      bottomNavDisplay: bottomNav ? getComputedStyle(bottomNav).display : 'missing',
      desktopNavDisplay: desktopNav ? getComputedStyle(desktopNav).display : 'missing',
    };
  });
  const expectedSections = ['hero','price_filters','categories','arrived_today','worth_it','nearby','bundles','office_delivery','home_delivery','free_delivery','area_stores','new_stores','featured_campaigns'];
  if (JSON.stringify(report.sections) !== JSON.stringify(expectedSections)) await observations.fail(`home_sections_${viewport.width}`);
  assert(report.dir === 'rtl' && report.lang === 'ar', `rtl_default_failed_${viewport.width}`);
  assert(report.scrollWidth <= report.clientWidth + 1, `horizontal_overflow_${viewport.width}_${report.scrollWidth}`);
  assert(report.heroBackground.includes('bisa-hero-v1.webp'), `hero_webp_missing_${viewport.width}`);
  assert(report.products > 0 && report.stores > 0 && report.bundles > 0, `catalog_surface_missing_${viewport.width}`);
  assert(report.tooSmall.length === 0, `touch_targets_${viewport.width}:${JSON.stringify(report.tooSmall.slice(0, 12))}`);
  if (viewport.width >= 1024) {
    assert(report.bottomNavDisplay === 'none', 'desktop_bottom_nav_visible');
    assert(report.desktopNavDisplay !== 'none', 'desktop_nav_missing');
  } else {
    assert(report.bottomNavDisplay !== 'none', `mobile_bottom_nav_missing_${viewport.width}`);
  }
  await page.locator('#languageButton').click();
  await page.locator('html[dir="ltr"][lang="en"]').waitFor();
  const englishFit = await page.evaluate(() => ({scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth}));
  assert(englishFit.scroll <= englishFit.client + 1, `ltr_horizontal_overflow_${viewport.width}`);
  await page.locator('#languageButton').click();
  await page.locator('html[dir="rtl"][lang="ar"]').waitFor();
  assert(observations.consoleErrors.length === 0, `console_errors_${viewport.width}:${observations.consoleErrors.join('|')}`);
  return {width: viewport.width, rtl: true, ltr: true, sections: report.sections.length, touchTargets: true, noOverflow: true};
}

async function assertExploreAndDetails(page, observations) {
  await page.locator('#shopperNav [data-navigate="explore"]').click();
  await page.locator('.explore-toolbar').waitFor();
  await page.locator('[data-open-filters]').click();
  const dialog = page.locator('#overlayRoot .dialog');
  await dialog.waitFor();
  const firstFocusable = dialog.locator('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled])').first();
  const lastFocusable = dialog.locator('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled])').last();
  await firstFocusable.focus();
  await page.keyboard.press('Shift+Tab');
  assert(await lastFocusable.evaluate(node => node === document.activeElement), 'dialog_focus_trap_failed');
  await page.keyboard.press('Escape');
  await dialog.waitFor({state: 'hidden'});
  await page.locator('[data-display="map"]').click();
  await page.locator('.map-surface').waitFor();
  assert(await page.locator('.map-pin').count() === 0, 'simulated_map_pins_must_not_render');
  await page.locator('.map-surface[data-bisa-map-state="ready"]').waitFor();
  assert(await page.locator('.bisa-map-marker').count() > 0, 'interactive_branch_markers_missing');
  assert(await page.locator('.bisa-map-attribution').isVisible(), 'map_attribution_missing');
  assert(await page.locator('.bisa-map-report-link').isVisible(), 'map_report_link_missing');
  await page.locator('[data-display="list"]').click();
  const firstProduct = page.locator('[data-open-product]').first();
  await firstProduct.waitFor();
  await firstProduct.click();
  await page.locator('.product-detail').waitFor();
  await page.goBack();
  await page.locator('.explore-toolbar').waitFor();
  assert(observations.consoleErrors.length === 0, `explore_console_errors:${observations.consoleErrors.join('|')}`);
}

async function signInThroughUi(page, role, phone, pin) {
  await page.goto(`${new URL(page.url()).origin}/?view=account`, {waitUntil: 'domcontentloaded'});
  await page.locator(`[data-login-role="${role}"]`).click();
  const form = page.locator('#loginForm');
  await form.waitFor();
  await form.locator('[name="phone"]').fill(phone);
  await form.locator('[name="pin"]').fill(pin);
  await form.locator('button[type="submit"]').click();
  await page.locator('#overlayRoot').waitFor({state: 'hidden'});
}

async function shopperFlow(browser, base) {
  const context = await browser.newContext({viewport: {width: 390, height: 844}});
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const observations = observePage(page);
  let checkoutRequest = null;
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/checkout') {
      checkoutRequest = {body: request.postDataJSON(), idempotencyKey: request.headers()['idempotency-key']};
    }
  });
  await page.goto(base, {waitUntil: 'domcontentloaded'});
  await page.locator('[data-add-kind="product"]').first().waitFor();
  const candidates = await page.locator('[data-add-kind="product"]').evaluateAll(buttons => {
    const rows = [];
    for (const button of buttons) {
      const row = {itemId: button.dataset.itemId, branch: button.dataset.branch};
      if (!rows.some(item => item.itemId === row.itemId && item.branch === row.branch)) rows.push(row);
    }
    return rows;
  });
  if (candidates.length <= 1) {
    const firstCard = await page.locator('.product-card').first().evaluate(node => node.outerHTML).catch(() => 'missing');
    await observations.fail(`shopper_products_missing:${JSON.stringify({candidates,firstCard})}`);
  }
  await centeredClick(page.locator(`[data-add-kind="product"][data-item-id="${candidates[0].itemId}"][data-branch="${candidates[0].branch}"]`).first());
  await page.locator('[data-navigate="cart"]:visible').first().click();
  await page.locator('.cart-item').waitFor();
  await page.locator('[data-review-checkout]').click();
  const login = page.locator('#loginForm');
  await login.waitFor();
  await login.locator('[name="phone"]').fill('96890000001');
  await login.locator('[name="pin"]').fill('1234');
  await login.locator('button[type="submit"]').click();
  await page.locator('#checkoutForm').waitFor();
  await page.keyboard.press('Escape');
  await page.locator('.cart-item').waitFor();
  const before = Number(await page.locator('.quantity-control output').first().textContent());
  await page.locator('[data-cart-action="increment"]').first().click();
  await page.waitForFunction(previous => Number(document.querySelector('.quantity-control output')?.textContent) === previous + 1, before);

  await page.locator('[data-navigate="home"]:visible').first().click();
  const alternate = candidates.find(item => item.branch && item.branch !== candidates[0].branch);
  assert(Boolean(alternate), 'cross_store_candidate_missing');
  await centeredClick(page.locator(`[data-add-kind="product"][data-item-id="${alternate.itemId}"][data-branch="${alternate.branch}"]`).first());
  await page.locator('.comparison').waitFor();
  await page.locator('[data-close-dialog]').last().click();
  await page.locator('#overlayRoot').waitFor({state: 'hidden'});

  await page.locator('[data-navigate="cart"]:visible').first().click();
  const delivery = page.locator('input[name="fulfillmentMode"]:not([value="pickup"])');
  let deliveryAddressReview = false;
  if (await delivery.count()) {
    await delivery.first().check();
    await page.locator('[data-review-checkout]').click();
    await page.locator('#checkoutForm [name="wilayahId"]').waitFor();
    assert(await page.locator('#checkoutForm [name="areaId"]').count() === 1, 'checkout_area_missing');
    assert(await page.locator('#checkoutForm [name="addressText"]').count() === 1, 'checkout_address_text_missing');
    deliveryAddressReview = true;
    await page.keyboard.press('Escape');
    await page.locator('input[name="fulfillmentMode"][value="pickup"]').check();
  }
  await page.locator('[data-review-checkout]').click();
  const checkout = page.locator('#checkoutForm');
  await checkout.waitFor();
  await checkout.locator('button[type="submit"]').click();
  await page.locator('.timeline').waitFor();
  assert(new URL(page.url()).searchParams.get('view') === 'order-detail', 'checkout_did_not_open_order');
  assert(checkoutRequest?.body?.paymentMethod === 'pay_at_store', 'pickup_payment_method_mismatch');
  assert(checkoutRequest?.body?.fulfillmentMode === 'pickup', 'pickup_fulfillment_mismatch');
  assert(checkoutRequest?.body?.idempotencyKey === checkoutRequest?.idempotencyKey, 'checkout_idempotency_header_mismatch');
  await page.locator('[data-navigate="account"]:visible').first().click();
  await page.locator('[data-navigate="addresses"]').click();
  await page.locator('[data-address-add]').waitFor();
  await page.locator('[data-address-add]').click();
  await page.locator('#addressForm [name="wilayahId"]').waitFor();
  await page.keyboard.press('Escape');
  const unexpectedHttp = observations.httpErrors.filter(row => !row.includes(' 409 ') || !row.endsWith('/api/cart/items'));
  assert(observations.consoleErrors.filter(message => !message.startsWith('Failed to load resource:')).length === 0 && unexpectedHttp.length === 0, `shopper_console_errors:${JSON.stringify({console:observations.consoleErrors,http:observations.httpErrors})}`);
  await context.close();
  return {login: true, editableCart: true, crossStoreDialog: true, checkoutReview: true, deliveryAddressReview, idempotentCheckout: true, orderDetail: true, addresses: true};
}

async function merchantFlow(browser, base) {
  const context = await browser.newContext({viewport: {width: 390, height: 844}});
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const observations = observePage(page);
  await page.goto(base, {waitUntil: 'domcontentloaded'});
  await signInThroughUi(page, 'merchant_owner', '96892000003', '1234');
  await page.locator('.merchant-hero').waitFor();
  assert(await page.locator('#merchantNav button').count() === 5, 'merchant_mobile_nav_count');
  const responsive = [];
  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await page.goto(`${base}/?view=merchant-today`, {waitUntil: 'domcontentloaded'});
    await page.locator('.merchant-hero').waitFor();
    const fit = await page.evaluate(() => ({scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth, mobileNav: getComputedStyle(document.querySelector('#merchantNav')).display, desktopNav: getComputedStyle(document.querySelector('#desktopNav')).display}));
    assert(fit.scroll <= fit.client + 1, `merchant_horizontal_overflow_${viewport.width}`);
    if (viewport.width >= 1024) assert(fit.desktopNav !== 'none' && fit.mobileNav === 'none', 'merchant_desktop_navigation_failed');
    else assert(fit.mobileNav !== 'none', `merchant_mobile_navigation_failed_${viewport.width}`);
    responsive.push({width: viewport.width, noOverflow: true});
  }
  await page.setViewportSize({width: 390, height: 844});
  await page.locator('[data-navigate="merchant-catalog"]:visible').first().click();
  await page.locator('.catalog-toolbar').waitFor();
  await page.locator('[data-open="quick-product"]').first().click();
  await page.locator('#productForm').waitFor();
  await page.keyboard.press('Escape');
  await page.locator('[data-product-action]').first().click();
  await page.locator('#merchantProductActionForm').waitFor();
  await page.keyboard.press('Escape');
  await page.locator('[data-open="stock-check"]').click();
  await page.locator('#stockForm').waitFor();
  await page.keyboard.press('Escape');
  await page.locator('[data-navigate="merchant-more"]:visible').first().click();
  await page.locator('.menu-list').waitFor();
  await page.locator('[data-merchant-setting="branches"]').click();
  await page.locator('[data-open="merchant-branch-create"]').click();
  const branchForm = page.locator('#merchantBranchForm');
  await branchForm.locator('[name="branchCreateNameAr"]').fill('فرع اختبار الواجهة');
  await branchForm.locator('[name="branchCreateNameEn"]').fill('UI test branch');
  const branchWilayah = branchForm.locator('[name="branchCreateWilayah"]');
  await branchWilayah.selectOption({index: 1});
  const branchArea = branchForm.locator('[name="areaId"] option:not([hidden])').nth(1);
  await branchForm.locator('[name="areaId"]').selectOption(await branchArea.getAttribute('value'));
  await branchForm.locator('[name="address"]').fill('عنوان تجريبي خاص لاختبار العقد');
  await branchForm.locator('button[type="submit"]').click();
  await page.locator('#overlayRoot').waitFor({state: 'hidden'});
  await page.getByText(/فرع اختبار الواجهة|UI test branch/).first().waitFor();
  await page.locator('[data-navigate="merchant-more"]:visible').first().click();
  await page.locator('[data-merchant-setting="fulfillment"]').click();
  await page.locator('[data-merchant-fulfillment]').first().click();
  const fulfillmentForm = page.locator('#merchantFulfillmentForm');
  const deliveryAreas = fulfillmentForm.locator('[name="zoneAreas"]');
  if (await deliveryAreas.count()) await deliveryAreas.first().check();
  await fulfillmentForm.locator('button[type="submit"]').click();
  await page.locator('#overlayRoot').waitFor({state: 'hidden'});
  await page.locator('[data-navigate="merchant-more"]:visible').first().click();
  await page.locator('[data-merchant-setting="returns"]').click();
  await page.locator('[data-open="merchant-return-policy"]').click();
  const policyForm = page.locator('#merchantReturnPolicyForm');
  await policyForm.locator('[name="conditions"]').fill('الإرجاع وفق حقوق المستهلك وحالة المنتج.');
  await policyForm.locator('button[type="submit"]').click();
  await page.locator('#overlayRoot').waitFor({state: 'hidden'});
  await page.locator('[data-navigate="merchant-more"]:visible').first().click();
  await page.locator('[data-merchant-setting="team"]').click();
  await page.locator('[data-open="merchant-team-add"]').click();
  const teamForm = page.locator('#merchantTeamForm');
  await teamForm.locator('[name="teamAccountPhone"]').fill('96890000001');
  await teamForm.locator('[name="role"]').selectOption('merchant_staff');
  await teamForm.locator('button[type="submit"]').click();
  await page.locator('#overlayRoot').waitFor({state: 'hidden'});
  await page.locator('[data-navigate="merchant-promotions"]:visible').first().click();
  await page.locator('[data-open="campaign-create"]').click();
  await page.locator('#campaignForm').waitFor();
  await page.keyboard.press('Escape');
  assert(observations.consoleErrors.length === 0, `merchant_console_errors:${observations.consoleErrors.join('|')}`);
  await context.close();
  return {login: true, dashboard: true, catalog: true, productActions: true, stock: true, promotions: true, more: true, branchCreate: true, fulfillment: true, returnPolicy: true, team: true, responsive};
}

function seedAdmin(dbPath) {
  const salt = 'bisa-ui-test-salt';
  const digest = pbkdf2Sync('2468', salt, 210000, 32, 'sha256').toString('hex');
  const hash = `pbkdf2_sha256$210000$${salt}$${digest}`;
  const code = [
    'import sqlite3,sys',
    'db,secret=sys.argv[1],sys.argv[2]',
    'con=sqlite3.connect(db)',
    "con.execute(\"INSERT OR REPLACE INTO accounts(id,phone,name,pin_hash,status,created_at) VALUES('ui_admin','96897777776','UI Admin',?,'active','2026-08-09T00:00:00+00:00')\",(secret,))",
    "con.execute(\"INSERT OR REPLACE INTO account_roles(account_id,role,merchant_id,active) VALUES('ui_admin','admin','',1)\")",
    'con.commit();con.close()',
  ].join(';');
  const result = spawnSync('python', ['-c', code, dbPath, hash], {encoding: 'utf8'});
  if (result.status !== 0) throw new Error(`admin_seed_failed:${result.stderr}`);
}

async function adminFlow(browser, base, dbPath) {
  seedAdmin(dbPath);
  const loginResponse = await fetch(`${base}/api/auth`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phone: '96897777776', pin: '2468', role: 'admin', deviceId: 'ui-test'}),
  });
  const login = await loginResponse.json();
  assert(loginResponse.ok, `admin_login_failed:${JSON.stringify(login)}`);
  const context = await browser.newContext({viewport: {width: 1280, height: 800}});
  await context.addInitScript(auth => {
    localStorage.setItem('bisa.auth.token.v2', auth.token);
    localStorage.setItem('bisa.auth.account.v2', JSON.stringify(auth.account));
  }, {token: login.token, account: login.account});
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const observations = observePage(page);
  await page.goto(base, {waitUntil: 'domcontentloaded'});
  await page.locator('.admin-page .workspace-rail').waitFor();
  const adminNavCount = await page.locator('.admin-page .workspace-rail button').count();
  assert(adminNavCount >= 16, `admin_navigation_count:${adminNavCount}`);
  for (const view of ['admin-locations','admin-suppliers','admin-supplier-campaigns']) {
    assert(await page.locator(`[data-navigate="${view}"]`).count() === 1, `admin_route_missing:${view}`);
  }
  await page.locator('[data-navigate="admin-applications"]').first().click();
  await page.locator('.admin-toolbar').waitFor();
  assert(await page.locator('[data-admin-create]').count() === 0, 'untyped_admin_create_control_visible');
  assert(await page.locator('#adminResourceForm').count() === 0, 'generic_admin_form_must_not_exist');
  const reviews = page.locator('[data-admin-review]');
  if (await reviews.count()) {
    await reviews.first().click();
    await page.locator('#adminDecisionForm').waitFor();
    assert(await page.locator('.admin-document-row').count() >= 0, 'document_review_surface_missing');
    await page.keyboard.press('Escape');
  }
  await page.locator('[data-navigate="admin-locations"]').click();
  await page.locator('[data-open="admin-location-create"]').waitFor();
  await page.locator('[data-open="admin-location-create"]').click();
  await page.locator('#adminLocationForm').waitFor();
  assert(await page.locator('#adminLocationForm select[name="locationParent"] option').count() >= 7, 'location_master_wilayats_missing');
  await page.keyboard.press('Escape');
  await page.locator('[data-open="admin-location-import"]').click();
  await page.locator('#adminLocationImportForm').waitFor();
  await page.keyboard.press('Escape');
  await page.locator('[data-navigate="admin-suppliers"]').click();
  await page.locator('[data-open="admin-supplier-create"]').waitFor();
  await page.locator('[data-open="admin-supplier-create"]').click();
  const supplierForm = page.locator('#adminSupplierForm');
  await supplierForm.locator('[name="supplierAdminNameAr"]').fill('مورد اختبار الواجهة');
  await supplierForm.locator('[name="supplierAdminNameEn"]').fill('UI test supplier');
  await supplierForm.locator('[name="supplierAccountPhone"]').fill('96890000001');
  await supplierForm.locator('[name="reason"]').fill('UI authorization test');
  const supplierValidity = await supplierForm.evaluate(form => ({valid: form.checkValidity(), invalid: [...form.elements].filter(field => typeof field.checkValidity === 'function' && !field.checkValidity()).map(field => ({name: field.name, message: field.validationMessage, value: field.value}))}));
  assert(supplierValidity.valid, `admin_supplier_form_invalid:${JSON.stringify(supplierValidity.invalid)}`);
  const supplierRequest = page.waitForResponse(response => response.url().includes('/api/admin/resources/supplier/create'));
  await supplierForm.locator('button[name="action"][value="create"]').click();
  const supplierResponse = await supplierRequest;
  assert(supplierResponse.ok(), `admin_supplier_create_failed:${supplierResponse.status()}:${await supplierResponse.text()}`);
  try { await page.locator('#overlayRoot').waitFor({state: 'hidden'}); }
  catch { await observations.fail('admin_supplier_form_did_not_close'); }
  await page.getByText(/مورد اختبار الواجهة|UI test supplier/).first().waitFor();
  await page.locator('[data-navigate="admin-categories"]').click();
  await page.locator('[data-open="admin-category-create"]').click();
  const categoryForm = page.locator('#adminCategoryForm');
  await categoryForm.locator('[name="categoryAdminNameAr"]').fill('قسم اختبار الواجهة');
  await categoryForm.locator('[name="categoryAdminNameEn"]').fill('UI test category');
  await categoryForm.locator('[name="categoryAdminSlug"]').fill('ui-test-category');
  await categoryForm.locator('[name="categoryAdminSort"]').fill('999');
  await categoryForm.locator('button[value="create"]').click();
  await page.locator('#overlayRoot').waitFor({state: 'hidden'});
  await page.getByText(/قسم اختبار الواجهة|UI test category/).first().waitFor();
  await page.locator('[data-navigate="admin-plans"]').click();
  await page.locator('[data-admin-plan-edit]').first().click();
  await page.locator('#adminPlanForm [data-admin-plan-save]').click();
  await page.locator('#overlayRoot').waitFor({state: 'hidden'});
  await page.locator('[data-navigate="admin-settings"]').click();
  await page.locator('[data-open="admin-settings-edit"]').click();
  await page.locator('#adminSettingsForm [data-admin-settings-save]').click();
  await page.locator('#overlayRoot').waitFor({state: 'hidden'});
  assert(observations.consoleErrors.length === 0, `admin_console_errors:${observations.consoleErrors.join('|')}`);
  await context.close();
  return {overview: true, navigation: adminNavCount, queue: true, typedControlsOnly: true, documentReview: true, locationMaster: true, supplierProvisioning: true, categories: true, plans: true, settings: true};
}

async function supplierFlow(browser, base) {
  const context = await browser.newContext({viewport: {width: 390, height: 844}});
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const observations = observePage(page);
  await page.goto(base, {waitUntil: 'domcontentloaded'});
  await signInThroughUi(page, 'supplier_advertiser', '96890000001', '1234');
  await page.locator('[data-navigate="supplier-campaigns"]:visible').first().click();
  await page.locator('[data-open="supplier-campaign-create"]').click();
  const form = page.locator('#supplierCampaignForm');
  await form.locator('[name="supplierTitleAr"]').fill('عرض المورد للمتاجر');
  await form.locator('[name="supplierTitleEn"]').fill('Supplier offer for merchants');
  await form.locator('[name="wholesaleDescriptionAr"]').fill('عرض جملة مخصص للمتاجر المعتمدة في بيسا.');
  await form.locator('[name="wholesaleDescriptionEn"]').fill('Wholesale offer for approved BISA merchants.');
  await form.locator('[name="offerAr"]').fill('صندوق مختار للتجار');
  await form.locator('[name="offerEn"]').fill('Curated merchant case');
  await form.locator('[name="minimumOrderQuantity"]').fill('12');
  await form.locator('[name="creative"]').setInputFiles(path.join(root, 'assets', 'images', 'bisa-hero-v1.webp'));
  await form.locator('[name="startsAt"]').fill('2027-01-01T08:00');
  await form.locator('[name="endsAt"]').fill('2027-02-01T20:00');
  await form.locator('[name="termsAr"]').fill('تخضع الكمية للتوفر عند تأكيد العرض.');
  await form.locator('[name="termsEn"]').fill('Quantity is subject to confirmation.');
  const supplierCampaignValidity = await form.evaluate(formNode => {const button=formNode.querySelector('button[type="submit"]');return {valid:formNode.checkValidity(),buttonType:button?.type,formId:button?.form?.getAttribute('id'),invalid:[...formNode.elements].filter(field=>typeof field.checkValidity==='function'&&!field.checkValidity()).map(field=>({name:field.name,message:field.validationMessage,value:field.value}))};});
  assert(supplierCampaignValidity.valid && supplierCampaignValidity.buttonType === 'submit' && supplierCampaignValidity.formId === 'supplierCampaignForm', `supplier_campaign_form_invalid:${JSON.stringify(supplierCampaignValidity)}`);
  await form.locator('button[type="submit"]').click();
  try { await page.locator('#overlayRoot').waitFor({state: 'hidden'}); }
  catch { await observations.fail('supplier_campaign_form_did_not_close'); }
  const submit = page.locator('[data-supplier-campaign-submit]').first();
  await submit.waitFor();
  assert(await submit.isEnabled(), 'supplier_campaign_not_ready_for_review');
  await submit.click();
  await page.getByText(/بانتظار المراجعة|Pending review/).first().waitFor();
  assert(observations.consoleErrors.length === 0, `supplier_console_errors:${observations.consoleErrors.join('|')}`);
  await context.close();
  return {login: true, draft: true, privateCreative: true, submittedForReview: true};
}

async function supplierReviewFlow(browser, base) {
  const loginResponse = await fetch(`${base}/api/auth`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phone: '96897777776', pin: '2468', role: 'admin', deviceId: 'ui-review-test'}),
  });
  const login = await loginResponse.json();
  assert(loginResponse.ok, `admin_review_login_failed:${JSON.stringify(login)}`);
  const context = await browser.newContext({viewport: {width: 1280, height: 800}});
  await context.addInitScript(auth => {
    localStorage.setItem('bisa.auth.token.v2', auth.token);
    localStorage.setItem('bisa.auth.account.v2', JSON.stringify(auth.account));
  }, {token: login.token, account: login.account});
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const observations = observePage(page);
  await page.goto(`${base}/?view=admin-supplier-campaigns`, {waitUntil: 'domcontentloaded'});
  const review = page.locator('[data-admin-supplier-review]').first();
  await review.waitFor();
  await review.click();
  await page.locator('.campaign-review img').waitFor();
  await page.locator('#adminModerationForm [name="reason"]').fill('Creative, targeting and terms reviewed in full');
  await page.locator('#adminModerationForm button[value="approve"]').click();
  await page.locator('#overlayRoot').waitFor({state: 'hidden'});
  assert(observations.consoleErrors.length === 0, `supplier_review_console_errors:${observations.consoleErrors.join('|')}`);
  await context.close();
  return {queue: true, authorizedCreative: true, fullReview: true, approved: true};
}

async function pushPreferencesFlow(browser, base) {
  const context = await browser.newContext({viewport: {width: 390, height: 844}});
  const endpoint = 'https://fcm.googleapis.com/fcm/send/ui-push-token';
  const publicKey = Buffer.concat([Buffer.from([4]), Buffer.alloc(64, 7)]).toString('base64url');
  const p256dh = Buffer.concat([Buffer.from([4]), Buffer.alloc(64, 9)]).toString('base64url');
  const auth = Buffer.alloc(16, 11).toString('base64url');
  await context.addInitScript(values => {
    let permission = 'default';
    let subscription = null;
    const record = {permissionRequests: 0, subscribeCalls: 0, unsubscribeCalls: 0};
    const browserSubscription = {
      endpoint: values.endpoint,
      toJSON: () => ({endpoint: values.endpoint, keys: {p256dh: values.p256dh, auth: values.auth}}),
      unsubscribe: async () => {record.unsubscribeCalls += 1; return true;},
    };
    const pushManager = {
      getSubscription: async () => subscription,
      subscribe: async options => {
        if (!options?.userVisibleOnly || !(options.applicationServerKey instanceof Uint8Array)) throw new Error('invalid_subscribe_options');
        record.subscribeCalls += 1;
        subscription = browserSubscription;
        return subscription;
      },
    };
    const registration = {pushManager};
    const serviceWorker = {
      register: async () => registration,
      getRegistration: async () => registration,
      addEventListener: () => {},
    };
    Object.defineProperty(window, 'PushManager', {configurable: true, value: function PushManager() {}});
    Object.defineProperty(window, 'Notification', {configurable: true, value: {
      get permission() { return permission; },
      requestPermission: async () => {record.permissionRequests += 1; permission = 'granted'; return permission;},
    }});
    Object.defineProperty(navigator, 'serviceWorker', {configurable: true, value: serviceWorker});
    window.__bisaPushTest = record;
  }, {endpoint, p256dh, auth});

  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const observations = observePage(page);
  let bound = false;
  let subscribeRequests = 0;
  let unsubscribeRequests = 0;
  const logoutBodies = [];
  await page.route('**/assets/scripts/bisa-app.js', route => route.fulfill({status: 200, contentType: 'text/javascript; charset=utf-8', body: fs.readFileSync(path.join(root, 'assets', 'scripts', 'bisa-app.js'), 'utf8')}));
  await page.route('**/api/push/**', async route => {
    const request = route.request();
    if (request.method() === 'GET') {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, available: true, configured: true, status: 'ready', publicKey, errorCode: '', activeForCurrentRole: bound, role: 'shopper'})});
      return;
    }
    const body = request.postDataJSON() || {};
    if (request.method() === 'POST') {
      subscribeRequests += 1;
      bound = body.endpoint === endpoint && Boolean(body.keys?.p256dh) && Boolean(body.keys?.auth);
      await route.fulfill({status: bound ? 200 : 422, contentType: 'application/json', body: JSON.stringify(bound ? {ok: true, active: true} : {ok: false, error: 'valid_push_subscription_required'})});
      return;
    }
    if (request.method() === 'DELETE') {
      unsubscribeRequests += 1;
      if (body.endpoint === endpoint) bound = false;
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, deactivated: 1})});
      return;
    }
    await route.fulfill({status: 405, contentType: 'application/json', body: JSON.stringify({ok: false})});
  });
  await page.route('**/api/auth/logout', async route => {
    const body = route.request().postDataJSON() || {};
    logoutBodies.push(body);
    if (body.endpoint === endpoint) bound = false;
    await route.continue();
  });

  await page.goto(base, {waitUntil: 'domcontentloaded'});
  assert(await page.evaluate(() => window.__bisaPushTest.permissionRequests) === 0, 'push_permission_requested_on_initial_load');
  const signInWithoutReload = async () => {
    await page.locator('[data-login-role="shopper"]').click();
    const form = page.locator('#loginForm');
    await form.waitFor();
    await form.locator('[name="phone"]').fill('96890000001');
    await form.locator('[name="pin"]').fill('1234');
    await form.locator('button[type="submit"]').click();
    await page.locator('#overlayRoot').waitFor({state: 'hidden'});
  };
  await page.locator('[data-navigate="account"]:visible').first().click();
  await signInWithoutReload();
  assert(await page.evaluate(() => window.__bisaPushTest.permissionRequests) === 0, 'push_permission_requested_during_login');
  await page.locator('[data-navigate="account-settings"]').click();
  const enable = page.locator('[data-push-enable]');
  try { await enable.waitFor(); }
  catch {
    const support = await page.evaluate(() => ({secure: window.isSecureContext, pushManager: 'PushManager' in window, notification: 'Notification' in window, permission: window.Notification?.permission, serviceWorker: 'serviceWorker' in navigator, subtle: Boolean(window.crypto?.subtle), pushTest: window.__bisaPushTest, text: document.body.innerText.slice(-1200)}));
    await observations.fail(`push_enable_missing:${JSON.stringify(support)}`);
  }
  await enable.click();
  await page.locator('[data-push-disable]').waitFor();
  assert(await page.evaluate(() => window.__bisaPushTest.permissionRequests) === 1, 'push_permission_not_explicit_or_repeated');
  assert(await page.evaluate(() => window.__bisaPushTest.subscribeCalls) === 1, 'browser_push_subscribe_count_mismatch');
  assert(subscribeRequests === 1 && bound, 'server_push_binding_not_confirmed');

  await page.locator('[data-push-disable]').click();
  await page.locator('[data-push-enable]').waitFor();
  assert(unsubscribeRequests === 1 && !bound, 'role_scoped_push_disable_not_confirmed');
  assert(await page.evaluate(() => window.__bisaPushTest.unsubscribeCalls) === 0, 'shared_browser_subscription_was_destroyed');

  await page.locator('[data-push-enable]').click();
  await page.locator('[data-push-disable]').waitFor();
  assert(await page.evaluate(() => window.__bisaPushTest.permissionRequests) === 1, 'existing_permission_was_prompted_again');
  await page.locator('[data-navigate="account"]:visible').first().click();
  await page.locator('[data-logout]').click();
  await page.waitForURL(url => !url.searchParams.has('view'));
  await page.locator('[data-navigate="account"]:visible').first().click();
  await page.locator('[data-login-role="shopper"]').waitFor();
  assert(logoutBodies.some(body => body.endpoint === endpoint), 'logout_did_not_send_push_endpoint_best_effort');
  await signInWithoutReload();
  for (let attempt = 0; attempt < 40 && subscribeRequests < 3; attempt += 1) await sleep(50);
  assert(subscribeRequests >= 3 && bound, 'existing_subscription_not_rebound_after_login');
  assert(await page.evaluate(() => window.__bisaPushTest.permissionRequests) === 1, 'background_rebind_requested_permission');

  const worker = fs.readFileSync(path.join(root, 'service-worker.js'), 'utf8');
  const appScript = fs.readFileSync(path.join(root, 'assets', 'scripts', 'bisa-app.js'), 'utf8');
  assert(worker.includes("pushsubscriptionchange"), 'push_subscription_change_handler_missing');
  assert(worker.includes('You have a new update in BISA.'), 'generic_english_push_copy_missing');
  assert(worker.includes('لديك تحديث جديد في بيسا.'), 'generic_arabic_push_copy_missing');
  assert(appScript.includes("kind==='branch-review'&&role==='admin'"), 'admin_branch_review_notification_route_missing');
  assert(appScript.includes("kind==='branch'&&role==='merchant'"), 'merchant_branch_notification_route_missing');
  assert(observations.consoleErrors.filter(message => !message.startsWith('Failed to load resource:')).length === 0, `push_console_errors:${observations.consoleErrors.join('|')}`);
  await context.close();
  return {permissionOnlyOnClick: true, enabledAndConfirmed: true, roleScopedDisable: true, sharedSubscriptionPreserved: true, logoutEndpoint: true, reboundAfterLogin: true, bilingualGenericPayload: true, branchDeepLinks: true};
}

(async () => {
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  const dbPath = path.join(tmp, 'bisa.sqlite3');
  const env = {
    ...process.env,
    BISA_ENV: 'development',
    BISA_DATA_DIR: tmp,
    BISA_DB_PATH: dbPath,
    BISA_UPLOAD_DIR: path.join(tmp, 'uploads'),
    BISA_BACKUP_DIR: path.join(tmp, 'backups'),
    BISA_SEED_SAMPLE_DATA: 'true',
    BISA_DEMO_PIN: '1234',
    BISA_AUTH_PEPPER: `ui-test-auth-${'a'.repeat(40)}`,
    BISA_MEDIA_SIGNING_KEY: `ui-test-media-${'m'.repeat(40)}`,
    HOST: '127.0.0.1', PORT: String(port), PYTHONIOENCODING: 'utf-8',
  };
  let stdout = '';
  let stderr = '';
  const server = spawn('python', ['bisa_server.py'], {cwd: root, env, stdio: ['ignore', 'pipe', 'pipe']});
  server.stdout.on('data', chunk => stdout += chunk.toString());
  server.stderr.on('data', chunk => stderr += chunk.toString());
  const diagnostics = () => `${stdout}\n${stderr}`;
  let browser;
  try {
    await waitForServer(base, diagnostics);
    browser = await chromium.launch({headless: true});
    if (process.env.BISA_UI_ONLY === 'push') {
      const push = await pushPreferencesFlow(browser, base);
      console.log(JSON.stringify({ok: true, push}));
      return;
    }
    const responsive = [];
    for (const viewport of viewports) {
      const context = await browser.newContext({viewport});
      const page = await context.newPage();
      page.setDefaultTimeout(15000);
      const observations = observePage(page);
      await page.goto(base, {waitUntil: 'domcontentloaded'});
      responsive.push(await assertHome(page, viewport, observations));
      if (viewport.width === 390) await assertExploreAndDetails(page, observations);
      await context.close();
    }
    const shopper = await shopperFlow(browser, base);
    const merchant = await merchantFlow(browser, base);
    const admin = await adminFlow(browser, base, dbPath);
    const supplier = await supplierFlow(browser, base);
    const supplierReview = await supplierReviewFlow(browser, base);
    const push = await pushPreferencesFlow(browser, base);
    console.log(JSON.stringify({ok: true, responsive, shopper, merchant, admin, supplier, supplierReview, push}));
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    await Promise.race([new Promise(resolve => server.once('exit', resolve)), sleep(2500)]);
    fs.rmSync(tmp, {recursive: true, force: true});
  }
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
