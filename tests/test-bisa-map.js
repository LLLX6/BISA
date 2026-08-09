'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

class FakeClassList {
  constructor(owner) { this.owner = owner; this.values = new Set(); }
  reset(value) { this.values = new Set(String(value || '').split(/\s+/).filter(Boolean)); }
  add(...values) { values.forEach(value => this.values.add(value)); }
  remove(...values) { values.forEach(value => this.values.delete(value)); }
  contains(value) { return this.values.has(value); }
  toggle(value, force) {
    const enabled = force === undefined ? !this.values.has(value) : Boolean(force);
    if (enabled) this.values.add(value); else this.values.delete(value);
    return enabled;
  }
}

class FakeElement {
  constructor(document) {
    this.nodeType = 1;
    this.ownerDocument = document;
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.classList = new FakeClassList(this);
    this._className = '';
    this.textContent = '';
  }
  set className(value) { this._className = String(value || ''); this.classList.reset(this._className); }
  get className() { return this._className; }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  replaceChildren(...children) { this.children = children; children.forEach(child => { child.parentNode = this; }); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
}

class FakeDocument {
  constructor() { this.documentElement = { dir: 'rtl' }; }
  createElement() { return new FakeElement(this); }
}

function makeLeaflet(document) {
  const calls = { maps: [], tiles: [], markers: [] };
  const leaflet = {
    latLngBounds(points) { return { points }; },
    map(element, options) {
      const map = {
        element, options, removed: false, invalidated: false, view: null, fitted: null,
        setView(point, zoom, viewOptions) { this.view = { point, zoom, viewOptions }; return this; },
        fitBounds(bounds, fitOptions) { this.fitted = { bounds, fitOptions }; return this; },
        invalidateSize() { this.invalidated = true; },
        remove() { this.removed = true; }
      };
      calls.maps.push(map);
      return map;
    },
    tileLayer(template, options) {
      const tile = { template, options, addTo(map) { this.map = map; return this; } };
      calls.tiles.push(tile);
      return tile;
    },
    divIcon(options) { return { options }; },
    marker(point, options) {
      const element = document.createElement('div');
      element.className = options.icon.options.className;
      const marker = {
        point, options, element, handlers: {},
        addTo(map) { this.map = map; calls.markers.push(this); return this; },
        on(name, callback) { this.handlers[name] = callback; return this; },
        getElement() { return this.element; }
      };
      return marker;
    }
  };
  return { leaflet, calls };
}

function loadModule({ leaflet = true } = {}) {
  const document = new FakeDocument();
  const mock = makeLeaflet(document);
  global.window = {
    document,
    location: { origin: 'https://bisa.test' },
    setTimeout(callback) { callback(); },
    ...(leaflet ? { L: mock.leaflet } : {})
  };
  const modulePath = path.resolve(__dirname, '../assets/scripts/bisa-map.js');
  delete require.cache[require.resolve(modulePath)];
  require(modulePath);
  return { document, calls: mock.calls, BisaMap: global.window.BisaMap };
}

const provider = {
  available: true,
  locale: 'ar',
  tileUrlTemplate: 'https://tile.example.test/{z}/{x}/{y}.png',
  attributionHtml: '&copy; <a href="https://example.test/copyright">Map contributors</a>',
  reportIssueUrl: 'https://example.test/report'
};

{
  const { document, BisaMap } = loadModule();
  const element = document.createElement('div');
  const result = BisaMap.mount({ element, stores: [], config: { ...provider, available: false } });
  assert.equal(result.mounted, false);
  assert.equal(result.reason, 'provider_unavailable');
  assert.equal(element.getAttribute('data-bisa-map-state'), 'provider_unavailable');
  assert.equal(element.children[0].getAttribute('role'), 'status');
  assert.equal(BisaMap.destroy(), true);
  assert.equal(element.children.length, 0);
}

{
  const { document, calls, BisaMap } = loadModule();
  const element = document.createElement('div');
  const selected = [];
  const stores = [
    { id: 'branch-1', name_ar: 'فرع السيب', latitude: 23.60, longitude: 58.20 },
    { id: 'branch-2', name_ar: 'فرع بوشر', latitude: '23.57', longitude: '58.43' },
    { id: 'missing-pin', name_ar: 'بلا موقع' },
    { id: 'outside-muscat', name_ar: 'خارج النطاق', latitude: 20.0, longitude: 56.0 }
  ];
  const result = BisaMap.mount({
    element, stores, config: provider, selectedId: 'branch-1', onSelect: store => selected.push(store.id)
  });
  assert.deepEqual(result, { mounted: true, reason: 'ready', visibleStoreCount: 2, hiddenStoreCount: 2 });
  assert.equal(element.getAttribute('data-bisa-map-state'), 'ready');
  assert.equal(calls.maps.length, 1);
  assert.equal(calls.tiles[0].template, provider.tileUrlTemplate);
  assert.equal(calls.tiles[0].options.keepBuffer, 0);
  assert.equal(calls.markers.length, 2);
  assert.equal(calls.markers[0].element.getAttribute('role'), 'button');
  assert.equal(calls.markers[0].element.getAttribute('aria-pressed'), 'true');
  calls.markers[1].handlers.click();
  assert.deepEqual(selected, ['branch-2']);
  assert.equal(calls.markers[1].element.getAttribute('aria-pressed'), 'true');
  assert.ok(calls.maps[0].fitted, 'multiple branches should use a bounded fit');
  BisaMap.destroy();
  assert.equal(calls.maps[0].removed, true);
}

{
  const { document, BisaMap } = loadModule({ leaflet: false });
  const element = document.createElement('div');
  const result = BisaMap.mount({ element, stores: [], config: provider });
  assert.equal(result.reason, 'leaflet_missing');
}

{
  const source = fs.readFileSync(path.resolve(__dirname, '../assets/scripts/bisa-map.js'), 'utf8');
  assert.doesNotMatch(source, /navigator\s*\.|geolocation|serviceWorker|\bfetch\s*\(/);
  assert.match(source, /keepBuffer:\s*0/);
}

delete global.window;
console.log('BISA map unit tests passed: 4/4');
