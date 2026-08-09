'use strict';

const fs = require('fs');
const path = require('path');
const {spawnSync} = require('child_process');
const {createHash} = require('crypto');

const root = path.resolve(__dirname, '..');
const shellFiles = ['index.html', 'assets/styles/bisa.css', 'assets/scripts/bisa-app.js'];
const mapFiles = ['assets/scripts/bisa-map.js', 'vendor/leaflet.js', 'vendor/leaflet.css'];
const mirroredFiles = [...shellFiles, ...mapFiles, 'assets/images/bisa-hero-v1.webp'];
const read = file => fs.readFileSync(path.join(root, file));
const sha256 = buffer => createHash('sha256').update(buffer).digest('hex');
const assert = (condition, message) => {if (!condition) throw new Error(message);};

const files = shellFiles.map(file => ({file, bytes: read(file).length, sha256: sha256(read(file))}));
const publicShellBytes = files.reduce((sum, item) => sum + item.bytes, 0);
assert(publicShellBytes <= 380000, `public_shell_budget_exceeded:${publicShellBytes}`);
const mapBundleBytes = mapFiles.reduce((sum, file) => sum + read(file).length, 0);
assert(mapBundleBytes <= 180000, `map_bundle_budget_exceeded:${mapBundleBytes}`);

const hero = read('assets/images/bisa-hero-v1.webp');
assert(hero.length <= 100000, `hero_webp_budget_exceeded:${hero.length}`);
assert(read('assets/styles/bisa.css').toString('utf8').includes('bisa-hero-v1.webp'), 'optimized_hero_not_referenced');

for (const source of mirroredFiles) {
  const mirror = source === 'index.html' ? 'public/index.html' : `public/${source}`;
  assert(fs.existsSync(path.join(root, mirror)), `mirror_missing:${mirror}`);
  assert(read(source).equals(read(mirror)), `mirror_mismatch:${source}:${mirror}`);
}

const html = read('index.html').toString('utf8');
const script = read('assets/scripts/bisa-app.js').toString('utf8');
assert(!/\sstyle\s*=/.test(html) && !/\sstyle\s*=/.test(script), 'inline_style_breaks_csp');
assert(!script.includes('demoBootstrap'), 'production_demo_bootstrap_forbidden');
assert(!script.includes('refreshToken'), 'refresh_token_must_not_be_managed_by_javascript');
assert(!script.includes('data:image/'), 'base64_media_payload_forbidden');
assert(!script.includes('map-pin'), 'simulated_map_pin_forbidden');
assert(!script.includes("apiFallback('/api/merchant/promotions'"), 'merchant_settings_promotion_fallback_forbidden');
assert(script.includes("paymentMethod=mode==='pickup'?'pay_at_store':'cash_on_delivery'"), 'checkout_payment_mapping_missing');
assert(script.includes("selected.has(day)?[{open,close}]:[]"), 'opening_hours_slots_contract_missing');
assert(script.includes("setInterval(()=>{if(state.account"), 'notification_polling_missing');

const syntax = spawnSync(process.execPath, ['--check', path.join(root, 'assets/scripts/bisa-app.js')], {encoding: 'utf8'});
assert(syntax.status === 0, `javascript_syntax_failed:${syntax.stderr}`);

console.log(JSON.stringify({
  ok: true,
  publicShellBytes,
  shellBudgetBytes: 380000,
  mapBundleBytes,
  mapBundleBudgetBytes: 180000,
  heroWebpBytes: hero.length,
  heroBudgetBytes: 100000,
  mirrors: mirroredFiles.length,
  files,
}));
