/* BISA interactive branch map. Requires the locally bundled Leaflet 1.9.4 runtime. */
(function installBisaMap(global) {
  'use strict';

  const DEFAULT_MUSCAT_BOUNDS = [[23.05, 57.95], [23.90, 59.30]];
  const DEFAULT_MUSCAT_CENTER = [23.5880, 58.3829];
  let active = null;

  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function clean(value, maxLength) {
    return String(value == null ? '' : value).replace(/[\u0000-\u001f\u007f]/g, ' ').trim().slice(0, maxLength);
  }

  function isElement(value) {
    return Boolean(value && value.nodeType === 1 && value.ownerDocument && typeof value.replaceChildren === 'function');
  }

  function language(config) {
    const locale = clean(config && config.locale, 12).toLowerCase();
    if (locale) return locale.startsWith('ar') ? 'ar' : 'en';
    return global.document && global.document.documentElement && global.document.documentElement.dir === 'rtl' ? 'ar' : 'en';
  }

  function message(config, arabic, english) {
    return language(config) === 'ar' ? arabic : english;
  }

  function normalizeBounds(value) {
    const candidate = Array.isArray(value) && value.length === 2 ? value : DEFAULT_MUSCAT_BOUNDS;
    const south = number(candidate[0] && candidate[0][0]);
    const west = number(candidate[0] && candidate[0][1]);
    const north = number(candidate[1] && candidate[1][0]);
    const east = number(candidate[1] && candidate[1][1]);
    if (south == null || west == null || north == null || east == null || south >= north || west >= east) {
      return DEFAULT_MUSCAT_BOUNDS;
    }
    return [[south, west], [north, east]];
  }

  function inBounds(lat, lng, bounds) {
    return lat >= bounds[0][0] && lat <= bounds[1][0] && lng >= bounds[0][1] && lng <= bounds[1][1];
  }

  function coordinates(store) {
    const lat = number(store && (store.latitude ?? store.lat));
    const lng = number(store && (store.longitude ?? store.lng ?? store.lon));
    if (lat == null || lng == null || lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
    return [lat, lng];
  }

  function storeId(store, index) {
    return clean(store && (store.branch_id ?? store.branchId ?? store.id), 100) || `branch-${index + 1}`;
  }

  function storeLabel(store, config) {
    const arabic = language(config) === 'ar';
    return clean(store && (
      (arabic && (store.name_ar ?? store.store_name_ar ?? store.branch_name_ar)) ||
      (!arabic && (store.name_en ?? store.store_name_en ?? store.branch_name_en)) ||
      store.name || store.store_name || store.branch_name || store.name_ar || store.name_en
    ), 140) || message(config, 'فرع متجر', 'Store branch');
  }

  function validTileTemplate(value) {
    const template = clean(value, 500);
    if (!template || !template.includes('{z}') || !template.includes('{x}') || !template.includes('{y}')) return '';
    try {
      const probe = new URL(template.replaceAll('{s}', 'a').replaceAll('{z}', '10').replaceAll('{x}', '1').replaceAll('{y}', '1'));
      if (probe.protocol !== 'https:' || probe.username || probe.password) return '';
      return template;
    } catch (_error) {
      return '';
    }
  }

  function safeLink(value) {
    const raw = clean(value, 500);
    if (!raw) return '';
    try {
      const origin = global.location && global.location.origin;
      const base = typeof origin === 'string' && /^https:\/\//i.test(origin) ? origin : 'https://bisa.invalid';
      const parsed = raw.startsWith('/') ? new URL(raw, base) : new URL(raw);
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password) return '';
      return raw.startsWith('/') ? `${parsed.pathname}${parsed.search}${parsed.hash}` : parsed.href;
    } catch (_error) {
      return '';
    }
  }

  function attribution(config) {
    const raw = clean(config && (config.attributionHtml || config.attribution || config.attributionText), 1000);
    if (!raw) return { text: '', href: '' };
    const hrefMatch = raw.match(/href\s*=\s*["']([^"']+)["']/i);
    const text = raw
      .replace(/<[^>]*>/g, ' ')
      .replace(/&copy;|&#169;/gi, '©')
      .replace(/&amp;/gi, '&')
      .replace(/&nbsp;/gi, ' ')
      .replace(/&#39;|&apos;/gi, "'")
      .replace(/&quot;/gi, '"')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 240);
    return { text, href: safeLink(hrefMatch && hrefMatch[1]) };
  }

  function appendText(parent, tagName, className, text) {
    const node = parent.ownerDocument.createElement(tagName);
    if (className) node.className = className;
    node.textContent = text;
    parent.appendChild(node);
    return node;
  }

  function appendExternalLink(parent, className, href, text) {
    if (!href) return null;
    const link = appendText(parent, 'a', className, text);
    link.href = href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  }

  function reportLink(parent, config) {
    return appendExternalLink(
      parent,
      'bisa-map-report-link',
      safeLink(config && config.reportIssueUrl),
      message(config, 'الإبلاغ عن مشكلة في الخريطة', 'Report a map issue')
    );
  }

  function renderUnavailable(element, config, reason) {
    const card = element.ownerDocument.createElement('div');
    card.className = 'bisa-map-status map-unavailable';
    card.setAttribute('role', 'status');
    appendText(card, 'span', 'bisa-map-status-icon', '⌖').setAttribute('aria-hidden', 'true');
    const title = reason === 'leaflet_missing'
      ? message(config, 'تعذر تشغيل الخريطة الآن', 'The map could not start')
      : message(config, 'الخريطة غير مفعّلة حالياً', 'The map is not enabled yet');
    appendText(card, 'h2', '', title);
    appendText(
      card,
      'p',
      '',
      message(
        config,
        'لم يتم إعداد مزود خرائط معتمد. ما زالت بيانات الفروع والقائمة متاحة، ولن نطلب موقع جهازك.',
        'An approved map provider is not configured. Branch details and the list remain available, and your device location is not requested.'
      )
    );
    reportLink(card, config);
    element.replaceChildren(card);
    element.classList.add('bisa-map-mounted', 'is-unavailable');
    element.setAttribute('data-bisa-map-state', reason);
    return { mounted: false, reason, visibleStoreCount: 0 };
  }

  function legalBar(shell, config, attributionMeta) {
    const bar = shell.ownerDocument.createElement('div');
    bar.className = 'bisa-map-legal';
    bar.setAttribute('aria-label', message(config, 'مصدر الخريطة والدعم', 'Map source and support'));
    if (attributionMeta.href) {
      appendExternalLink(bar, 'bisa-map-attribution', attributionMeta.href, attributionMeta.text);
    } else {
      appendText(bar, 'span', 'bisa-map-attribution', attributionMeta.text);
    }
    reportLink(bar, config);
    shell.appendChild(bar);
    return bar;
  }

  function mount(options) {
    const settings = options && typeof options === 'object' ? options : {};
    const element = settings.element;
    if (!isElement(element)) throw new TypeError('BisaMap.mount requires a DOM element');
    destroy();

    const config = settings.config && typeof settings.config === 'object' ? settings.config : {};
    const tileUrlTemplate = config.available === false ? '' : validTileTemplate(config.tileUrlTemplate);
    const attributionMeta = attribution(config);
    const reportIssueUrl = safeLink(config.reportIssueUrl);
    const leaflet = global.L;

    element.classList.remove('is-unavailable');
    element.setAttribute('aria-busy', 'false');
    if (!tileUrlTemplate || !attributionMeta.text || !reportIssueUrl) {
      active = { element, map: null };
      return renderUnavailable(element, config, 'provider_unavailable');
    }
    if (!leaflet || typeof leaflet.map !== 'function' || typeof leaflet.tileLayer !== 'function') {
      active = { element, map: null };
      return renderUnavailable(element, config, 'leaflet_missing');
    }

    const bounds = normalizeBounds(config.muscatBounds || config.bounds);
    const inputStores = Array.isArray(settings.stores) ? settings.stores : [];
    const visibleStores = inputStores.map((store, index) => {
      const point = coordinates(store);
      return point && inBounds(point[0], point[1], bounds)
        ? { id: storeId(store, index), label: storeLabel(store, config), point, store }
        : null;
    }).filter(Boolean);

    const shell = element.ownerDocument.createElement('div');
    shell.className = 'bisa-map-shell';
    const mapElement = element.ownerDocument.createElement('div');
    mapElement.className = 'bisa-map-canvas';
    mapElement.setAttribute('role', 'region');
    mapElement.setAttribute('aria-label', message(config, 'خريطة فروع المتاجر', 'Store branch map'));
    shell.appendChild(mapElement);
    legalBar(shell, config, attributionMeta);
    if (!visibleStores.length) {
      const empty = shell.ownerDocument.createElement('div');
      empty.className = 'bisa-map-empty';
      empty.setAttribute('role', 'status');
      appendText(empty, 'strong', '', message(config, 'لا توجد فروع على الخريطة', 'No branches on the map'));
      appendText(empty, 'span', '', message(config, 'جرّب تغيير عوامل التصفية.', 'Try changing the filters.'));
      shell.appendChild(empty);
    }
    element.replaceChildren(shell);
    element.classList.add('bisa-map-mounted');
    element.setAttribute('data-bisa-map-state', visibleStores.length ? 'ready' : 'empty');

    const mapBounds = leaflet.latLngBounds(bounds);
    const map = leaflet.map(mapElement, {
      attributionControl: false,
      center: DEFAULT_MUSCAT_CENTER,
      zoom: 10,
      minZoom: 9,
      maxZoom: 18,
      maxBounds: mapBounds,
      maxBoundsViscosity: 1,
      zoomControl: true,
      keyboard: true,
      preferCanvas: false
    });
    leaflet.tileLayer(tileUrlTemplate, {
      attribution: '',
      minZoom: 9,
      maxZoom: 18,
      noWrap: true,
      updateWhenIdle: true,
      updateWhenZooming: false,
      keepBuffer: 0
    }).addTo(map);

    const selectedId = clean(settings.selectedId, 100);
    const markers = [];
    const select = (entry) => {
      for (const item of markers) {
        const selected = item.entry.id === entry.id;
        const markerElement = item.marker.getElement && item.marker.getElement();
        if (markerElement) {
          markerElement.classList.toggle('is-selected', selected);
          markerElement.setAttribute('aria-pressed', selected ? 'true' : 'false');
        }
      }
      if (typeof settings.onSelect === 'function') settings.onSelect(entry.store);
    };

    for (const entry of visibleStores) {
      const selected = entry.id === selectedId;
      const marker = leaflet.marker(entry.point, {
        icon: leaflet.divIcon({
          className: `bisa-map-marker${selected ? ' is-selected' : ''}`,
          html: '<span class="bisa-map-marker-dot" aria-hidden="true"></span>',
          iconSize: [46, 52],
          iconAnchor: [23, 50]
        }),
        keyboard: true,
        riseOnHover: true,
        title: entry.label,
        alt: entry.label
      }).addTo(map);
      marker.on('click', () => select(entry));
      const markerElement = marker.getElement && marker.getElement();
      if (markerElement) {
        markerElement.setAttribute('role', 'button');
        markerElement.setAttribute('aria-label', message(config, `فتح ${entry.label}`, `Open ${entry.label}`));
        markerElement.setAttribute('aria-pressed', selected ? 'true' : 'false');
        markerElement.addEventListener('keydown', (event) => {
          if (event.key === ' ') {
            event.preventDefault();
            select(entry);
          }
        });
      }
      markers.push({ entry, marker });
    }

    if (visibleStores.length === 1) {
      map.setView(visibleStores[0].point, 14, { animate: false });
    } else if (visibleStores.length > 1) {
      map.fitBounds(leaflet.latLngBounds(visibleStores.map(entry => entry.point)), {
        animate: false,
        maxZoom: 15,
        padding: [28, 28]
      });
    } else {
      map.fitBounds(mapBounds, { animate: false, padding: [12, 12] });
    }

    active = { element, map, markers };
    global.setTimeout(() => {
      if (active && active.map === map && typeof map.invalidateSize === 'function') map.invalidateSize(false);
    }, 0);
    return {
      mounted: true,
      reason: visibleStores.length ? 'ready' : 'empty',
      visibleStoreCount: visibleStores.length,
      hiddenStoreCount: inputStores.length - visibleStores.length
    };
  }

  function destroy() {
    if (!active) return false;
    const current = active;
    active = null;
    if (current.map && typeof current.map.remove === 'function') current.map.remove();
    if (isElement(current.element)) {
      current.element.replaceChildren();
      current.element.classList.remove('bisa-map-mounted', 'is-unavailable');
      current.element.removeAttribute('data-bisa-map-state');
      current.element.removeAttribute('aria-busy');
    }
    return true;
  }

  global.BisaMap = Object.freeze({ mount, destroy });
}(typeof window !== 'undefined' ? window : globalThis));
