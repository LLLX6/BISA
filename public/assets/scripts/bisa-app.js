(() => {
  'use strict';

  const STORAGE = {
    token: 'bisa.auth.token.v1', account: 'bisa.auth.account.v1', language: 'bisa.language.v1',
    location: 'bisa.location.v1', cartIntent: 'bisa.cart.intent.v1', demoPurged: 'bisa.demo.purged.v1'
  };
  const STATIC_SHOWCASE = location.hostname.endsWith('github.io');
  const API = location.protocol === 'file:' ? 'http://127.0.0.1:8080' : '';
  const state = {
    lang: localStorage.getItem(STORAGE.language) || 'ar',
    token: localStorage.getItem(STORAGE.token) || '',
    account: parse(localStorage.getItem(STORAGE.account), null),
    location: parse(localStorage.getItem(STORAGE.location), {id:'muscat_governorate', name_ar:'مسقط', name_en:'Muscat'}),
    view: new URLSearchParams(location.search).get('view') || 'home', merchantView: 'today',
    bootstrap: {products:[],stores:[],bundles:[],advertisements:[],categories:[],locations:[],cart:null,notifications:[],orders:[]},
    dashboard: null, filters: {query:'',category:'',display:'list'}, loading: true
  };
  const $ = (selector, root=document) => root.querySelector(selector);
  const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
  const t = (ar, en) => state.lang === 'ar' ? ar : en;
  const esc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
  const fmt = value => `${Number(value || 0).toFixed(3)} ${t('ر.ع','OMR')}`;
  const productEmoji = id => ({storage:'🧺',kitchen:'🥤',snacks:'🥜',stationery:'📒',cleaning:'🧽',toys:'🪁',decor:'🪴',party:'🎉',accessories:'👜',personal:'🧴',car:'🚗',seasonal:'✨'})[id] || '✦';
  function parse(value, fallback){ try { return value ? JSON.parse(value) : fallback; } catch { return fallback; } }

  function demoBootstrap(){
    const regions=[
      ['muscat','مركز مسقط','Muscat Centre','لمسات الميناء','Harbour Finds','🪴'],
      ['muttrah','مركز مطرح','Muttrah Centre','لقطات السوق','Souq Pop','🛍️'],
      ['bawshar','الخوير','Al Khuwair','بيت بيسا','BISA Home','🏠'],
      ['seeb','الموالح','Al Mawaleh','يوميات الموالح','Mawaleh Daily','✨'],
      ['al_amerat','مركز العامرات','Al Amerat Centre','لمعة العامرات','Amerat Spark','🎁'],
      ['qurayyat','مركز قريات','Qurayyat Centre','اختيارات الساحل','Coast Picks','🌊']
    ];
    const categoryRows=[['storage','التخزين والتنظيم','Storage & Organization','🧺'],['kitchen','أدوات المطبخ','Kitchen','🍳'],['stationery','قرطاسية','Stationery','✏️'],['cleaning','التنظيف','Cleaning','🧽'],['snacks','مكسرات وسناكس','Snacks & Nuts','🥜'],['party','مستلزمات الحفلات','Party','🎉']];
    const templates=[['storage','منظم يومي','Daily organizer','1.300'],['kitchen','أكواب ملوّنة','Color cups','0.500'],['stationery','دفتر جيب','Pocket notebook','0.250'],['cleaning','إسفنجة عملية','Handy sponge','0.100'],['snacks','مكسرات مختارة','Selected nuts','1.900'],['party','زينة صغيرة','Mini party decor','2.000']];
    const locations=[{id:'muscat_governorate',parent_id:'oman',kind:'governorate',name_ar:'محافظة مسقط',name_en:'Muscat Governorate'}];
    const stores=[],products=[],bundles=[],advertisements=[];
    regions.forEach((region,index)=>{
      const [key,areaAr,areaEn,storeAr,storeEn,icon]=region; const areaId=`demo_area_${key}`,branchId=`demo_branch_${key}`,merchantId=`demo_merchant_${key}`;
      locations.push({id:`wilayat_${key}`,parent_id:'muscat_governorate',kind:'wilayat',name_ar:areaAr.replace('مركز ',''),name_en:areaEn.replace(' Centre','')});
      locations.push({id:areaId,parent_id:`wilayat_${key}`,kind:'area',name_ar:areaAr,name_en:areaEn});
      stores.push({merchant_id:merchantId,name_ar:storeAr,name_en:storeEn,verified:1,branch_id:branchId,branch_name_ar:`فرع ${areaAr}`,branch_name_en:`${areaEn} Branch`,area_id:areaId,address_text:areaAr,pickup_enabled:1,office_enabled:1,home_enabled:1,product_count:4});
      for(let n=0;n<4;n++){const [category,nameAr,nameEn,price]=templates[(index+n)%templates.length];products.push({id:`demo_product_${key}_${n+1}`,merchant_id:merchantId,category_id:category,name_ar:nameAr,name_en:nameEn,description_ar:`${icon} اكتشاف تجريبي في ${areaAr}`,description_en:`${icon} Demo discovery in ${areaEn}`,price,merchant_name_ar:storeAr,merchant_name_en:storeEn,verified:1,branch_id:branchId,branch_name_ar:`فرع ${areaAr}`,branch_name_en:`${areaEn} Branch`,area_id:areaId,quantity:25,availability:'in_stock',images:[]});}
      bundles.push({id:`demo_bundle_${key}`,merchant_id:merchantId,branch_id:branchId,title_ar:`باقة ${areaAr}`,title_en:`${areaEn} Bundle`,description:'باقة تجريبية مختارة',price:'3.100',normalValue:'3.300',merchant_name_ar:storeAr,merchant_name_en:storeEn,verified:1,area_id:areaId,component_count:3});
      advertisements.push({id:`demo_ad_${key}`,owner_id:merchantId,landing_id:branchId,area_id:areaId,label_ar:'إعلان تجريبي',label_en:'Demo sponsored',creative:{areaId,titleAr:`اكتشف ${storeAr}`,titleEn:`Discover ${storeEn}`,bodyAr:`اختيارات اليوم في ${areaAr}`,bodyEn:`Today's picks in ${areaEn}`,icon}});
    });
    const purged=localStorage.getItem(STORAGE.demoPurged)==='true';
    return {ok:true,demoMode:true,demoCounts:purged?{}:{merchant:6,product:24,bundle:6,ad:6,area:6},locations:purged?locations.slice(0,1):locations,categories:categoryRows.map((r,i)=>({id:r[0],name_ar:r[1],name_en:r[2],icon:r[3],sort_order:i})),stores:purged?[]:stores,products:purged?[]:products,bundles:purged?[]:bundles,advertisements:purged?[]:advertisements,plans:[],cart:purged?null:parse(sessionStorage.getItem('bisa.demo.cart'),null),orders:[],notifications:[],actor:state.account,settings:{commissionRate:0,paymentsEnabled:false}};
  }

  function inSelectedLocation(item){
    const selected=state.location||{}; if(!selected.id||selected.kind==='governorate'||selected.id==='muscat_governorate')return true;
    if(selected.kind==='area')return item.area_id===selected.id;
    if(selected.kind==='wilayat'){const area=(state.bootstrap.locations||[]).find(row=>row.id===item.area_id);return area?.parent_id===selected.id;}
    return true;
  }

  const copy = {
    ar:{navHome:'الرئيسية',navExplore:'استكشف',navCart:'السلة',navOrders:'طلباتي',navAccount:'حسابي',merchantToday:'اليوم',merchantOrders:'الطلبات',merchantCatalog:'الكتالوج',merchantPromotions:'الترويج',merchantMore:'المزيد'},
    en:{navHome:'Home',navExplore:'Explore',navCart:'Cart',navOrders:'Orders',navAccount:'Account',merchantToday:'Today',merchantOrders:'Orders',merchantCatalog:'Catalog',merchantPromotions:'Promote',merchantMore:'More'}
  };

  async function api(path, options={}){
    const headers = {'Content-Type':'application/json', ...(options.headers || {})};
    if(state.token) headers.Authorization = `Bearer ${state.token}`;
    let response;
    try { response = await fetch(`${API}${path}`, {...options, headers}); }
    catch { throw Object.assign(new Error('network_unavailable'), {code:'network_unavailable'}); }
    const data = await response.json().catch(() => ({}));
    if(!response.ok || data.ok === false) throw Object.assign(new Error(data.error || 'request_failed'), {code:data.error || 'request_failed', status:response.status, detail:data.detail || {}});
    return data;
  }

  function saveAuth(result){
    state.token = result.token; state.account = result.account;
    localStorage.setItem(STORAGE.token, state.token); localStorage.setItem(STORAGE.account, JSON.stringify(state.account));
  }
  function clearAuth(){ state.token=''; state.account=null; state.dashboard=null; localStorage.removeItem(STORAGE.token); localStorage.removeItem(STORAGE.account); }
  function toast(message){
    const node=document.createElement('div'); node.className='toast'; node.textContent=message; $('#toastRoot').append(node);
    setTimeout(() => node.remove(), 3300);
  }
  function niceError(error){
    const map={authentication_required:t('سجّل الدخول أولاً','Please sign in first'),invalid_login:t('بيانات الدخول غير صحيحة','Incorrect sign-in details'),valid_pin_required:t('أدخل رمزاً من 4 إلى 8 أرقام','Enter a 4–8 digit PIN'),valid_phone_required:t('أدخل رقم هاتف عمانياً صحيحاً','Enter a valid Omani phone number'),merchant_application_required:t('لا توجد مساحة تاجر معتمدة لهذا الحساب','No approved merchant workspace for this account'),cross_store_cart_confirmation_required:t('السلة لمتجر آخر','Your cart belongs to another store'),cart_empty:t('السلة فارغة','Your cart is empty'),stock_unavailable:t('بعض المنتجات لم تعد متوفرة','Some products are no longer available'),price_out_of_range:t('سعر المنتج يجب أن يكون بين 100 بيسة و2 ر.ع','Product price must be between 100 baisa and OMR 2'),active_plan_required:t('يلزم اشتراك تاجر نشط','An active merchant plan is required'),plan_product_limit:t('وصلت إلى حد المنتجات في باقتك','Your plan product limit is reached'),network_unavailable:t('تعذر الاتصال. تأكد أن خادم BISA يعمل','Could not connect. Make sure the BISA server is running')};
    return map[error.code] || t('تعذر إكمال العملية الآن','The action could not be completed');
  }

  async function load({quiet=false}={}){
    if(!quiet){ state.loading=true; render(); }
    if(STATIC_SHOWCASE){state.bootstrap=demoBootstrap();state.loading=false;render();return;}
    try {
      state.bootstrap = await api('/api/bootstrap');
      if(state.token && !state.bootstrap.actor){ clearAuth(); }
      if(state.account?.role?.startsWith('merchant_')) await loadDashboard();
    } catch(error){ if(!quiet) toast(niceError(error)); }
    state.loading=false; render();
  }
  async function loadDashboard(){
    try { state.dashboard = await api('/api/merchant/dashboard'); }
    catch(error){ state.dashboard=null; if(error.status!==401) toast(niceError(error)); }
  }

  function updateChrome(){
    document.documentElement.lang=state.lang; document.documentElement.dir=state.lang==='ar'?'rtl':'ltr';
    $('#languageButton').textContent=state.lang==='ar'?'EN':'ع';
    $('#locationLabel').textContent=state.location[state.lang==='ar'?'name_ar':'name_en'] || t('مسقط','Muscat');
    $$('[data-i18n]').forEach(node => node.textContent=copy[state.lang][node.dataset.i18n] || node.textContent);
    const merchant=state.account?.role?.startsWith('merchant_');
    $('#shopperNav').hidden=!!merchant; $('#merchantNav').hidden=!merchant;
    const cart=state.bootstrap.cart; const count=cart?.items?.reduce((n,item)=>n+Number(item.quantity||0),0)||0;
    $('#cartBadge').hidden=!count; $('#cartBadge').textContent=String(count);
    const actions=(state.bootstrap.notifications||[]).filter(n=>Number(n.requires_action)&&!n.acted_at).length;
    $('#notificationBadge').hidden=!actions; $('#notificationBadge').textContent=String(actions);
  }

  function setActiveNav(){
    $$('[data-view]').forEach(button => button.setAttribute('aria-current',button.dataset.view===state.view?'page':'false'));
    $$('[data-merchant-view]').forEach(button => button.setAttribute('aria-current',button.dataset.merchantView===state.merchantView?'page':'false'));
  }

  function render(){
    updateChrome(); setActiveNav();
    if(state.loading){ $('#viewRoot').innerHTML=loadingView(); return; }
    if(['admin','super_admin'].includes(state.account?.role)) return renderAdmin();
    if(state.view==='admin') { $('#viewRoot').innerHTML=gateView('◈',t('إدارة BISA','BISA administration'),t('هذه المساحة للمشرفين المخولين فقط.','This area is for authorized administrators only.'),'admin'); bindView(); return; }
    if(state.account?.role?.startsWith('merchant_')) return renderMerchant();
    const views={home:homeView,explore:exploreView,cart:cartView,orders:ordersView,account:accountView};
    $('#viewRoot').innerHTML=(views[state.view]||homeView)(); bindView();
  }

  function loadingView(){ return `<section class="page section"><div class="hero skeleton"></div><div class="product-grid">${[1,2,3,4].map(()=>'<div class="product-card skeleton loading-card"></div>').join('')}</div></section>`; }

  function homeView(){
    const products=(state.bootstrap.products||[]).filter(inSelectedLocation), stores=(state.bootstrap.stores||[]).filter(inSelectedLocation), bundles=(state.bootstrap.bundles||[]).filter(inSelectedLocation), advertisements=(state.bootstrap.advertisements||[]).filter(inSelectedLocation);
    return `<div class="page">
      ${state.bootstrap.demoMode?`<section class="demo-notice"><span>✦</span><div><b>${t('نسخة تجربة للهاتف','Phone demo')}</b><p>${t('المحلات والمنتجات والباقات والإعلانات تجريبية ويمكن حذفها من الإدارة.','Stores, products, bundles and ads are demo records removable from Admin.')}</p></div></section>`:''}
      <section class="hero">
        <div class="hero-copy"><p class="eyebrow">${t('اكتشاف محلي، بأسلوب جديد','Local discovery, reimagined')}</p>
          <h1>${t('اكتشافاتك،','Your discoveries,')} <span>${t('قريبة منك.','close to you.')}</span></h1>
          <p>${t('منتجات مختارة من 100 بيسة إلى 2 ر.ع، من متاجر قريبة في مسقط.','Curated products from 100 baisa to OMR 2, from stores near you in Muscat.')}</p>
          <div class="hero-actions"><button class="primary-button" data-go="explore">${t('ابدأ الاستكشاف','Start exploring')} <span>←</span></button><button class="secondary-button" data-open="merchantIntro">${t('انضم كتاجر','Join as a merchant')}</button></div>
        </div><div class="hero-proof" aria-hidden="true"><span>✦</span><span>⌖</span></div>
      </section>
      ${advertisements.length?`<section class="section">${advertisementCard(advertisements[0])}</section>`:''}
      <section class="section"><div class="search-bar"><label class="search-field"><span>⌕</span><input id="homeSearch" value="${esc(state.filters.query)}" placeholder="${t('ابحث عن منتج أو متجر قريب','Search products or nearby stores')}" aria-label="${t('البحث','Search')}"></label><button class="filter-button" data-go="explore" aria-label="${t('التصفية','Filters')}">≡</button></div></section>
      ${categoriesHtml()}
      <section class="section"><div class="section-head"><div><p class="eyebrow">${t('بين 100 بيسة و2 ر.ع','From 100 baisa to OMR 2')}</p><h2>${t('وصلت اليوم','Arrived today')}</h2></div><button class="text-button" data-go="explore">${t('عرض الكل','See all')}</button></div>${products.length?`<div class="product-grid">${products.slice(0,8).map(productCard).join('')}</div>`:emptyCatalog()}</section>
      ${bundles.length?`<section class="section"><div class="section-head"><div><p class="eyebrow">${t('عدة اكتشافات معاً','More discoveries together')}</p><h2>${t('باقات المنطقة','Area bundles')}</h2></div></div><div class="store-strip">${bundles.map(bundleCard).join('')}</div></section>`:''}
      <section class="section"><div class="section-head"><div><p class="eyebrow">${t('متاجر موثوقة','Trusted stores')}</p><h2>${t('قريب منك الآن','Near you now')}</h2></div></div>${stores.length?`<div class="store-strip">${stores.map(storeCard).join('')}</div>`:emptyStores()}</section>
      <section class="section info-banner"><span>⌁</span><div><b>${t('الاستلام والتوصيل من المتجر','Pickup and store delivery')}</b><p>${t('بيسا لا تدّعي وجود أسطول توصيل. كل متجر يوضح خياراته ورسومه وموعده قبل التأكيد.','BISA does not claim to operate a fleet. Each store shows its options, fees and timing before confirmation.')}</p></div></section>
    </div>`;
  }

  function advertisementCard(ad){const c=ad.creative||{};return `<article class="demo-ad"><div><span>${esc(ad[state.lang==='ar'?'label_ar':'label_en'])}</span><p>${esc(c[state.lang==='ar'?'bodyAr':'bodyEn']||'')}</p><h2>${esc(c[state.lang==='ar'?'titleAr':'titleEn']||'')}</h2></div><strong aria-hidden="true">${esc(c.icon||'✦')}</strong></article>`;}
  function bundleCard(b){return `<article class="store-card bundle-card"><div class="store-avatar">🎁</div><div><div class="store-line">${b.verified?'<i class="verified">✓</i>':''}${esc(b[state.lang==='ar'?'merchant_name_ar':'merchant_name_en'])}</div><h3>${esc(b[state.lang==='ar'?'title_ar':'title_en'])}</h3><p>${b.component_count} ${t('مكوّنات','components')} · <strong class="price">${fmt(b.price)}</strong></p></div><div class="fulfillment-tags"><span>${t('القيمة','Value')} ${fmt(b.normalValue)}</span><button class="add-button" data-add-bundle="${esc(b.id)}" data-branch="${esc(b.branch_id)}" aria-label="${t('أضف الباقة','Add bundle')}">＋</button></div></article>`;}

  function categoriesHtml(){
    const rows=state.bootstrap.categories||[];
    return `<section class="section"><div class="section-head"><div><h2>${t('اختر مزاجك','Pick your mood')}</h2><p>${t('فئات خفيفة لاكتشاف أسرع','Focused categories for faster discovery')}</p></div></div><div class="category-row">${rows.map(c=>`<button class="category-card" data-category="${esc(c.id)}"><span>${esc(c.icon)}</span><b>${esc(c[state.lang==='ar'?'name_ar':'name_en'])}</b></button>`).join('')}</div></section>`;
  }

  function productCard(p){
    const name=p[state.lang==='ar'?'name_ar':'name_en']||p.name_ar; const store=p[state.lang==='ar'?'merchant_name_ar':'merchant_name_en']||'';
    return `<article class="product-card"><div class="product-visual"><span>${productEmoji(p.category_id)}</span><b class="availability">${p.quantity>2?t('متوفر','In stock'):t('كمية محدودة','Limited')}</b></div><div class="product-card-content"><h3>${esc(name)}</h3><div class="store-line">${p.verified?'<i class="verified">✓</i>':''}${esc(store)}</div><div class="price-row"><strong class="price">${fmt(p.price)}</strong><button class="add-button" data-add-product="${esc(p.id)}" data-branch="${esc(p.branch_id)}" aria-label="${t('أضف للسلة','Add to cart')}">＋</button></div></div></article>`;
  }
  function storeCard(s){ const name=s[state.lang==='ar'?'name_ar':'name_en']; return `<article class="store-card"><div class="store-avatar">${esc((name||'B').slice(0,1))}</div><div><div class="store-line">${s.verified?`<i class="verified">✓</i>${t('متجر موثّق','Verified store')}`:t('متجر محلي','Local store')}</div><h3>${esc(name)}</h3><p>${esc(s.address_text||t('داخل مسقط','In Muscat'))} · ${s.product_count||0} ${t('منتج','products')}</p></div><div class="fulfillment-tags">${s.pickup_enabled?`<span>${t('استلام','Pickup')}</span>`:''}${s.office_enabled?`<span>${t('توصيل مكتب','Office')}</span>`:''}${s.home_enabled?`<span>${t('توصيل منزل','Home')}</span>`:''}</div></article>`; }
  function emptyCatalog(){ return `<div class="empty-state"><div class="empty-icon">✦</div><h3>${t('الكتالوج يستعد للانطلاق','The catalog is getting ready')}</h3><p>${t('لن نعرض منتجات وهمية. تظهر المنتجات هنا بعد اعتماد المتجر وتأكيد مخزونه.','We do not show fake products. Products appear after store approval and inventory confirmation.')}</p></div>`; }
  function emptyStores(){ return `<div class="empty-state"><div class="empty-icon">⌖</div><h3>${t('المتاجر المعتمدة ستظهر هنا','Approved stores will appear here')}</h3><p>${t('المنطقة لا تصبح عامة إلا عندما يكون فيها فرع معتمد ونشط.','An area becomes public only when it has an approved, active branch.')}</p></div>`; }

  function exploreView(){
    const products=(state.bootstrap.products||[]).filter(inSelectedLocation).filter(p=>!state.filters.category||p.category_id===state.filters.category).filter(p=>!state.filters.query||JSON.stringify(p).toLowerCase().includes(state.filters.query.toLowerCase()));
    return `<div class="page"><header class="page-title"><p class="eyebrow">${t('كل ما حولك','Everything around you')}</p><h1>${t('استكشف بوضوح','Explore clearly')}</h1><p>${t('قارن المنتج والمتجر وطريقة الاستلام قبل أن تضيف للسلة.','Compare product, store and fulfillment before adding to cart.')}</p></header>
      <section class="section"><div class="search-bar"><label class="search-field"><span>⌕</span><input id="exploreSearch" value="${esc(state.filters.query)}" placeholder="${t('اسم المنتج أو المتجر','Product or store name')}" aria-label="${t('البحث','Search')}"></label><button class="filter-button" data-open="filters">≡</button></div><div class="chip-row filter-chips"><button class="chip ${!state.filters.category?'active':''}" data-category="">${t('الكل','All')}</button>${(state.bootstrap.categories||[]).map(c=>`<button class="chip ${state.filters.category===c.id?'active':''}" data-category="${esc(c.id)}">${esc(c[state.lang==='ar'?'name_ar':'name_en'])}</button>`).join('')}</div></section>
      <section class="section"><div class="section-head"><div><h2>${t('النتائج','Results')}</h2><p>${products.length} ${t('منتج','products')}</p></div><div class="segmented"><button data-display="list" class="${state.filters.display==='list'?'active':''}">${t('قائمة','List')}</button><button data-display="map" class="${state.filters.display==='map'?'active':''}">${t('خريطة','Map')}</button></div></div>${state.filters.display==='map'?mapView():products.length?`<div class="product-grid">${products.map(productCard).join('')}</div>`:emptyCatalog()}</section></div>`;
  }
  function mapView(){ return `<div class="map-placeholder" aria-label="${t('معاينة الخريطة','Map preview')}"><div class="map-pin"><span>B</span></div><div class="map-pin"><span>✦</span></div><div class="map-pin"><span>2</span></div></div>`; }

  function cartView(){
    const cart=state.bootstrap.cart;
    if(!state.account) return gateView('▱',t('سلتك تنتظرك','Your cart is waiting'),t('سجّل الدخول لحفظ اكتشافاتك ومتابعتها بين أجهزتك.','Sign in to keep discoveries synced across devices.'), 'shopper');
    if(!cart?.items?.length) return `<div class="page"><header class="page-title"><h1>${t('السلة','Cart')}</h1></header>${emptyCart()}</div>`;
    return `<div class="page"><header class="page-title"><p class="eyebrow">${t('من متجر واحد','One store at a time')}</p><h1>${t('سلتك','Your cart')}</h1><p>${t('لضمان توفر أسرع وتسليم أوضح، كل سلة تخص متجراً واحداً.','For clearer availability and fulfillment, each cart belongs to one store.')}</p></header>
      <section class="cart-card">${cart.items.map(item=>`<div class="cart-item"><div class="item-thumb">${item.item_kind==='bundle'?'🎁':'✦'}</div><div><h3>${esc(item.snapshot_name||item.item_id)}</h3><span class="muted">${item.unitPrice} ${t('ر.ع','OMR')}</span></div><div class="quantity"><button type="button" disabled>−</button><b>${item.quantity}</b><button type="button" disabled>＋</button></div></div>`).join('')}</section>
      <section class="section cart-card"><div class="section-head"><div><h2>${t('طريقة الاستلام','Fulfillment')}</h2><p>${t('الرسوم النهائية تظهر قبل التأكيد','Final fees appear before confirmation')}</p></div></div><div class="fulfillment-options"><label class="option-card"><input type="radio" name="fulfillment" value="pickup" checked><span>🏬</span><div><b>${t('استلام من المتجر','Store pickup')}</b><small>${t('بدون رسوم توصيل','No delivery fee')}</small></div></label><label class="option-card"><input type="radio" name="fulfillment" value="office_delivery"><span>🏢</span><div><b>${t('توصيل إلى المكتب','Office delivery')}</b><small>${t('بحسب نطاق ورسوم المتجر','Store zone and fee apply')}</small></div></label><label class="option-card"><input type="radio" name="fulfillment" value="home_delivery"><span>🏠</span><div><b>${t('توصيل إلى المنزل','Home delivery')}</b><small>${t('بحسب نطاق ورسوم المتجر','Store zone and fee apply')}</small></div></label></div></section>
      <section class="section cart-card"><div class="summary-row"><span>${t('المجموع الفرعي','Subtotal')}</span><b>${cart.subtotal} ${t('ر.ع','OMR')}</b></div><div class="summary-row"><span>${t('رسوم التوصيل','Delivery fee')}</span><span>${t('تحسب عند التأكيد','Calculated at confirmation')}</span></div><div class="summary-row total"><span>${t('الإجمالي','Total')}</span><b>${cart.subtotal} ${t('ر.ع','OMR')}</b></div><button class="primary-button full" data-checkout>${t('أرسل الطلب للمتجر','Send order to store')}</button><p class="helper">${t('لا يتم الدفع الآن. المتجر يؤكد توفر المنتجات أولاً.','No payment is taken now. The store confirms availability first.')}</p></section></div>`;
  }
  function emptyCart(){ return `<div class="empty-state"><div class="empty-icon">▱</div><h2>${t('سلتك خفيفة الآن','Your cart is light')}</h2><p>${t('اكتشف منتجاً قريباً واضغط علامة الإضافة.','Discover something nearby and tap add.')}</p><button class="primary-button" data-go="explore">${t('ابدأ الاستكشاف','Start exploring')}</button></div>`; }

  function ordersView(){
    if(!state.account) return gateView('▤',t('طلباتك في مكان واحد','All your orders in one place'),t('سجّل الدخول لمتابعة تأكيد المتجر والاستلام.','Sign in to track store confirmation and fulfillment.'),'shopper');
    const orders=state.bootstrap.orders||[];
    return `<div class="page"><header class="page-title"><p class="eyebrow">${t('واضحة خطوة بخطوة','Clear, step by step')}</p><h1>${t('طلباتي','My orders')}</h1></header><section class="section">${orders.length?orders.map(orderCard).join(''):`<div class="empty-state"><div class="empty-icon">▤</div><h2>${t('لا توجد طلبات بعد','No orders yet')}</h2><p>${t('بعد إرسال أول طلب يظهر هنا مع حالته والموعد التالي.','Your first order will appear here with its status and next step.')}</p><button class="primary-button" data-go="explore">${t('استكشف الآن','Explore now')}</button></div>`}</section></div>`;
  }
  function orderCard(order){
    const status={pending_store_confirmation:t('بانتظار تأكيد المتجر','Waiting for store'),accepted:t('تم التأكيد','Confirmed'),rejected:t('غير متوفر','Unavailable'),ready:t('جاهز للاستلام','Ready'),completed:t('مكتمل','Completed')}[order.status]||order.status;
    const index={pending_store_confirmation:1,accepted:2,ready:3,completed:4}[order.status]||1;
    return `<article class="order-card stacked-card"><div class="section-head"><div><p class="eyebrow">${esc(order.id)}</p><h2>${esc(status)}</h2></div><strong class="price">${fmt((order.total_baisa||0)/1000)}</strong></div><div class="timeline">${[t('أُرسل للمتجر','Sent to store'),t('أكد المتجر','Store confirmed'),t('جاهز لك','Ready for you'),t('اكتمل','Completed')].map((label,i)=>`<div class="timeline-step ${i+1<index?'done':i+1===index?'current':''}"><b>${label}</b><small>${i+1===index?t('الخطوة الحالية','Current step'):''}</small></div>`).join('')}</div></article>`;
  }

  function accountView(){
    if(!state.account) return gateView('◎',t('مساحتك في بيسا','Your BISA space'),t('حساب واحد للاكتشاف والطلبات، ويمكنك التقديم كتاجر لاحقاً.','One account for discovery and orders, with the option to apply as a merchant.'),'shopper');
    return `<div class="page"><header class="page-title"><p class="eyebrow">${t('أهلاً','Welcome')} ${esc(state.account.name)}</p><h1>${t('حسابي','My account')}</h1></header><section class="account-card"><div class="store-card account-profile"><div class="store-avatar">${esc((state.account.name||'B').slice(0,1))}</div><div><h3>${esc(state.account.name)}</h3><p>${t('متسوق بيسا','BISA shopper')}</p></div></div></section><section class="section accordion"><details open><summary>${t('اكتشافاتي','My discovery')}</summary><div class="accordion-content">${t('المفضلة والمتاجر المتابعة ستظهر هنا عند تفعيلها.','Favorites and followed stores will appear here when enabled.')}</div></details><details><summary>${t('الإشعارات والخصوصية','Notifications & privacy')}</summary><div class="accordion-content">${t('الإشعارات الداخلية تعمل دون إذن Push. لن نعرض تفاصيل الطلب الحساسة على شاشة القفل.','In-app notifications work without Push permission. Sensitive order details are never shown on the lock screen.')}</div></details><details><summary>${t('اللغة والمظهر','Language & appearance')}</summary><div class="accordion-content"><button class="secondary-button" data-toggle-language>${t('التبديل إلى الإنجليزية','Switch to Arabic')}</button></div></details><details><summary>${t('التجارة عبر بيسا','Sell on BISA')}</summary><div class="accordion-content"><p>${t('قدّم طلب متجرك خطوة بخطوة. لا يصبح المتجر عاماً قبل المراجعة.','Apply step by step. Your store remains private until reviewed.')}</p><button class="primary-button" data-open="merchantApply">${t('ابدأ طلب متجر','Start merchant application')}</button></div></details><details><summary>${t('الأمان','Security')}</summary><div class="accordion-content"><button class="danger-button" data-logout>${t('تسجيل الخروج','Sign out')}</button></div></details></section></div>`;
  }
  function gateView(icon,title,body,role){ return `<div class="page"><header class="page-title"><h1>${esc(title)}</h1></header><div class="empty-state"><div class="empty-icon">${icon}</div><p>${esc(body)}</p><button class="primary-button" data-login-role="${role}">${t('تسجيل الدخول','Sign in')}</button></div></div>`; }

  function renderMerchant(){
    const views={today:merchantToday,merchantOrders:merchantOrders,catalog:merchantCatalog,promotions:merchantPromotions,merchantMore:merchantMore};
    $('#viewRoot').innerHTML=`<div class="page">${(views[state.merchantView]||merchantToday)()}</div>`; bindView();
  }
  function merchantToday(){
    const d=state.dashboard; if(!d) return `<section class="section">${emptyMerchant()}</section>`;
    const pending=(d.orders||[]).filter(o=>o.status==='pending_store_confirmation');
    return `<section class="merchant-hero"><p class="eyebrow">${t('مساحة التاجر','Merchant workspace')}</p><h1>${t('صباح الاكتشافات','A day of discoveries')}</h1><p>${esc(d.merchant[state.lang==='ar'?'name_ar':'name_en'])} · ${t('خطة','Plan')} ${esc(d.plan?.[state.lang==='ar'?'name_ar':'name_en']||'')}</p></section><section class="metric-grid"><div class="metric"><strong>${pending.length}</strong><small>${t('طلبات تحتاج تأكيد','Orders need confirmation')}</small></div><div class="metric"><strong>${d.products?.length||0}</strong><small>${t('منتجات نشطة','Active products')}</small></div><div class="metric"><strong>${d.branches?.length||0}</strong><small>${t('فروع','Branches')}</small></div><div class="metric"><strong>${Number(d.plan?.price||0).toFixed(0)}</strong><small>${t('ر.ع / الباقة','OMR / plan')}</small></div></section><section class="section"><div class="section-head"><div><h2>${t('ابدأ من هنا','Start here')}</h2><p>${t('أهم إجراء أولاً، دون ازدحام','The most important action first')}</p></div></div><div class="quick-actions"><button class="quick-action ${pending.length?'needs-action':''}" data-merchant-go="merchantOrders"><span>▤</span><b>${pending.length?t('أكد طلباً جديداً','Confirm a new order'):t('الطلبات واضحة','Orders are clear')}</b><small>${pending.length?t(`لديك ${pending.length} بانتظارك`,`${pending.length} waiting for you`):t('لا يوجد إجراء عاجل','No urgent action')}</small></button><button class="quick-action" data-open="quickProduct"><span>＋</span><b>${t('أضف منتجاً بسرعة','Quick add product')}</b><small>${t('السعر والمخزون في شاشة واحدة','Price and stock in one screen')}</small></button><button class="quick-action" data-open="stockCheck"><span>✓</span><b>${t('أكد المخزون','Verify stock')}</b><small>${t('حافظ على دقة الظهور','Keep availability accurate')}</small></button><button class="quick-action" data-merchant-go="promotions"><span>◇</span><b>${t('روّج بوضوح','Promote clearly')}</b><small>${t('لا وعود أو أرقام وهمية','No fake claims or metrics')}</small></button></div></section>${pending.length?`<section class="section"><div class="section-head"><div><h2>${t('يحتاج قرارك','Needs your decision')}</h2></div></div>${pending.map(merchantOrderCard).join('')}</section>`:''}`;
  }
  function emptyMerchant(){ return `<div class="empty-state"><div class="empty-icon">🏪</div><h2>${t('مساحة التاجر غير متاحة','Merchant workspace unavailable')}</h2><p>${t('تأكد أن طلب المتجر معتمد وله اشتراك نشط.','Make sure the merchant application is approved and has an active plan.')}</p><button class="danger-button" data-logout>${t('تسجيل الخروج','Sign out')}</button></div>`; }
  function merchantOrders(){ const rows=state.dashboard?.orders||[]; return `<header class="page-title"><p class="eyebrow">${t('قرار واضح في وقته','A clear, timely decision')}</p><h1>${t('الطلبات','Orders')}</h1><p>${t('أكد توفر كل المكونات قبل القبول.','Confirm all component stock before accepting.')}</p></header><section class="section">${rows.length?rows.map(merchantOrderCard).join(''):`<div class="empty-state"><div class="empty-icon">▤</div><h2>${t('لا توجد طلبات','No orders')}</h2><p>${t('ستظهر الطلبات الحقيقية هنا فقط.','Only real orders appear here.')}</p></div>`}</section>`; }
  function merchantOrderCard(o){ return `<article class="order-card stacked-card"><div class="section-head"><div><p class="eyebrow">${esc(o.id)}</p><h2>${o.status==='pending_store_confirmation'?t('تحقق من التوفر','Check availability'):esc(o.status)}</h2><p>${t('المهلة','Due')} · ${new Date(o.response_due_at).toLocaleString(state.lang==='ar'?'ar-OM':'en-OM')}</p></div><strong class="price">${fmt((o.total_baisa||0)/1000)}</strong></div>${o.status==='pending_store_confirmation'?`<div class="hero-actions"><button class="primary-button" data-order-decision="accept" data-order-id="${esc(o.id)}">${t('متوفر — قبول','Available — accept')}</button><button class="danger-button" data-order-decision="reject" data-order-id="${esc(o.id)}">${t('غير متوفر','Unavailable')}</button></div>`:''}</article>`; }
  function merchantCatalog(){ const products=state.dashboard?.products||[]; return `<header class="page-title"><p class="eyebrow">${t('بسيط ودقيق','Simple and accurate')}</p><h1>${t('الكتالوج','Catalog')}</h1></header><section class="section"><div class="hero-actions"><button class="primary-button" data-open="quickProduct">＋ ${t('منتج جديد','New product')}</button><button class="secondary-button" data-open="stockCheck">✓ ${t('تأكيد المخزون','Verify stock')}</button></div></section><section class="section">${products.length?`<div class="product-grid">${products.map(p=>productCard({...p,merchant_name_ar:state.dashboard.merchant.name_ar,merchant_name_en:state.dashboard.merchant.name_en,verified:state.dashboard.merchant.verified})).join('')}</div>`:emptyCatalog()}</section>`; }
  function merchantPromotions(){ return `<header class="page-title"><p class="eyebrow">${t('ترويج مسؤول','Responsible promotion')}</p><h1>${t('الترويج','Promotions')}</h1></header><section class="metric-grid"><div class="metric"><strong>0</strong><small>${t('حملات نشطة','Active campaigns')}</small></div><div class="metric"><strong>0</strong><small>${t('مشاهدات موثقة','Verified views')}</small></div></section><section class="section empty-state"><div class="empty-icon">◇</div><h2>${t('الإعلانات المدفوعة غير مفعّلة','Paid promotions are not enabled')}</h2><p>${t('لن نسجّل نتيجة أو خصماً قبل ربط بوابة دفع ومراجعة الحملة.','No result or charge is recorded until payment and review adapters are connected.')}</p></section>`; }
  function merchantMore(){ return `<header class="page-title"><p class="eyebrow">${t('إدارة أعمالك','Manage your business')}</p><h1>${t('المزيد','More')}</h1></header><section class="accordion"><details open><summary>${t('المتجر والفروع','Store & branches')}</summary><div class="accordion-content">${t('بيانات المتجر ومناطق التغطية وساعات العمل.','Store details, coverage zones and opening hours.')}</div></details><details><summary>${t('الفريق والصلاحيات','Team & permissions')}</summary><div class="accordion-content">${t('تحدد الخطة عدد الموظفين، وكل إجراء محمي من الخادم.','Your plan limits staff seats; every action is server-authorized.')}</div></details><details><summary>${t('الباقة والاشتراك','Plan & subscription')}</summary><div class="accordion-content">${t('الاشتراك التجريبي والأساسي والمتقدم دون عمولة حالياً.','Trial, Basic and Advanced plans with zero commission initially.')}</div></details><details><summary>${t('مركز الموردين','Supplier Hub')}</summary><div class="accordion-content">${t('خاص بالتجار المعتمدين. الحملات لا تظهر للمستهلك.','For approved merchants only. Supplier campaigns are never shown to consumers.')}</div></details><details><summary>${t('الدعم والسياسات','Support & policies')}</summary><div class="accordion-content">${t('سياسة الاسترجاع المحفوظة مع الطلب لا تتجاوز حقوق المستهلك في عُمان.','The order policy snapshot never overrides Oman consumer rights.')}</div></details><details><summary>${t('الأمان','Security')}</summary><div class="accordion-content"><button class="danger-button" data-logout>${t('تسجيل الخروج من مساحة التاجر','Sign out of merchant workspace')}</button></div></details></section>`; }

  async function renderAdmin(){
    $('#shopperNav').hidden=true; $('#merchantNav').hidden=true;
    let overview;
    try{ overview=STATIC_SHOWCASE?{counts:{merchants:state.bootstrap.stores.length,products:state.bootstrap.products.length,bundles:state.bootstrap.bundles.length,ads:state.bootstrap.advertisements.length},demoCounts:state.bootstrap.demoCounts||{},pendingApplications:[]}:await api('/api/admin/overview'); }
    catch(error){ $('#viewRoot').innerHTML=gateView('◈',t('تعذر فتح الإدارة','Administration unavailable'),niceError(error),'admin'); bindView(); return; }
    const pending=overview.pendingApplications||[];
    const demoTotal=Object.values(overview.demoCounts||{}).reduce((sum,value)=>sum+Number(value||0),0);
    $('#viewRoot').innerHTML=`<div class="page"><header class="page-title"><p class="eyebrow">${t('وصول مقيّد ومسجّل','Restricted and audited access')}</p><h1>${t('إدارة BISA','BISA Admin')}</h1><p>${t('الاعتماد من الخادم، وكل قرار يدخل سجل التدقيق.','Server-authorized decisions with a full audit trail.')}</p></header><section class="metric-grid">${Object.entries(overview.counts||{}).slice(0,4).map(([key,value])=>`<div class="metric"><strong>${value}</strong><small>${esc(key)}</small></div>`).join('')}</section><section class="section demo-admin-card"><div><p class="eyebrow">${t('بيانات العرض','Demo data')}</p><h2>${demoTotal?t(`${demoTotal} سجلاً تجريبياً جاهزاً للحذف`,`${demoTotal} demo records ready to remove`):t('لا توجد بيانات تجريبية','No demo data remains')}</h2><p>${t('يحذف الزر المحلات والمنتجات والباقات والإعلانات والحسابات التجريبية فقط، ولا يلمس البيانات الحقيقية.','The button removes only tagged demo stores, products, bundles, ads and accounts. Real data is preserved.')}</p></div><button class="danger-button" data-open="purgeDemo" ${demoTotal?'':'disabled'}>${t('حذف كل التجريبي','Delete all demo data')}</button></section><section class="section"><div class="section-head"><div><h2>${t('طلبات المتاجر','Merchant applications')}</h2><p>${pending.length} ${t('تحتاج مراجعة','need review')}</p></div></div>${pending.length?pending.map(app=>`<article class="order-card stacked-card"><p class="eyebrow">${esc(app.id)}</p><h3>${t('طلب متجر جديد','New merchant application')}</h3><p class="muted">${esc(app.status)}</p><div class="hero-actions"><button class="primary-button" data-admin-application="${esc(app.id)}" data-admin-decision="approve">${t('اعتماد وبدء التجربة','Approve & start trial')}</button><button class="secondary-button" data-admin-application="${esc(app.id)}" data-admin-decision="changes_requested">${t('طلب تحديث','Request changes')}</button><button class="danger-button" data-admin-application="${esc(app.id)}" data-admin-decision="reject">${t('رفض','Reject')}</button></div></article>`).join(''):`<div class="empty-state"><div class="empty-icon">✓</div><h3>${t('لا توجد طلبات معلقة','No pending applications')}</h3></div>`}</section><section class="section"><button class="danger-button" data-logout>${t('تسجيل خروج المشرف','Admin sign out')}</button></section></div>`;
  }

  function bindView(){
    const search=$('#homeSearch')||$('#exploreSearch'); if(search) search.addEventListener('keydown',e=>{if(e.key==='Enter'){state.filters.query=e.currentTarget.value.trim();state.view='explore';render();}});
  }

  function openSheet(title, eyebrow, html, onReady){
    const root=$('#sheetRoot'), fragment=$('#sheetTemplate').content.cloneNode(true); root.replaceChildren(fragment); root.hidden=false;
    $('#sheetTitle',root).textContent=title; $('#sheetEyebrow',root).textContent=eyebrow; $('#sheetContent',root).innerHTML=html;
    document.body.classList.add('sheet-open'); const focusable=$('input,button,select,textarea',root); focusable?.focus(); onReady?.(root);
  }
  function closeSheet(){ const root=$('#sheetRoot'); root.hidden=true; root.replaceChildren(); document.body.classList.remove('sheet-open'); }

  function loginSheet(role='shopper'){
    openSheet(t(role==='merchant_owner'?'دخول التاجر':'دخول بيسا',role==='merchant_owner'?'Merchant sign in':'Sign in to BISA'),t('حسابك محمي','Your account is protected'),`<form id="loginForm" class="form-grid"><input type="hidden" name="role" value="${role}"><div class="field"><label>${t('الاسم — للحساب الجديد','Name — for a new account')}</label><input name="name" maxlength="80" autocomplete="name"></div><div class="field"><label>${t('رقم الهاتف العُماني','Omani phone number')}</label><input name="phone" inputmode="tel" autocomplete="tel" placeholder="9XXXXXXX" required></div><div class="field"><label>${t('رمز الدخول','PIN')}</label><input name="pin" inputmode="numeric" autocomplete="current-password" minlength="4" maxlength="8" required><small class="helper">${t('من 4 إلى 8 أرقام. لا تشارك الرمز.','4–8 digits. Never share it.')}</small></div><button class="primary-button full" type="submit">${t('دخول آمن','Secure sign in')}</button>${role==='merchant_owner'?`<button class="text-button" type="button" data-login-role="shopper">${t('أدخل كمتسوق أولاً للتقديم','Sign in as shopper to apply first')}</button>`:''}</form>`,root=>{$('#loginForm',root).addEventListener('submit',loginSubmit);});
  }
  async function loginSubmit(event){ event.preventDefault(); const button=$('button[type="submit"]',event.currentTarget);button.disabled=true;const data=Object.fromEntries(new FormData(event.currentTarget));try{if(STATIC_SHOWCASE){state.account={id:'demo_preview',name:data.name||t('زائر التجربة','Demo visitor'),role:data.role||'shopper',merchantId:''};state.token='demo-preview';localStorage.setItem(STORAGE.account,JSON.stringify(state.account));localStorage.setItem(STORAGE.token,state.token);}else{saveAuth(await api('/api/auth',{method:'POST',body:JSON.stringify(data)}));}closeSheet();toast(t('أهلاً بك في بيسا','Welcome to BISA'));await load();}catch(error){toast(niceError(error));button.disabled=false;}}

  function merchantApplySheet(){
    if(!state.account) return loginSheet('shopper');
    const wilayats=(state.bootstrap.locations||[]).filter(l=>l.kind==='wilayat'); const areas=(state.bootstrap.locations||[]).filter(l=>l.kind==='area');
    openSheet(t('ابدأ مساحة متجرك','Start your store space'),t('طلب واضح، والمراجعة قبل النشر','Clear application, reviewed before publishing'),`<form id="merchantApplyForm" class="form-grid"><div class="form-row"><div class="field"><label>${t('اسم المتجر بالعربية','Store name in Arabic')}</label><input name="nameAr" required maxlength="100"></div><div class="field"><label>${t('Store name in English','اسم المتجر بالإنجليزية')}</label><input name="nameEn" dir="ltr" required maxlength="100"></div></div><div class="form-row"><div class="field"><label>${t('الولاية','Wilayat')}</label><select name="wilayahId" required><option value="">${t('اختر','Select')}</option>${wilayats.map(l=>`<option value="${esc(l.id)}">${esc(l[state.lang==='ar'?'name_ar':'name_en'])}</option>`).join('')}</select></div><div class="field"><label>${t('المنطقة — إن ظهرت','Area — if available')}</label><select name="areaId"><option value="">${t('تحدد لاحقاً','Set later')}</option>${areas.map(l=>`<option value="${esc(l.id)}">${esc(l[state.lang==='ar'?'name_ar':'name_en'])}</option>`).join('')}</select></div></div><div class="field"><label>${t('العنوان المختصر','Short address')}</label><input name="address" maxlength="240"></div><div class="field"><label>${t('رقم السجل التجاري','Commercial registration number')}</label><input name="crNumber" maxlength="50"></div><div class="field"><label>${t('وسيلة تواصل المالك','Owner contact')}</label><input name="ownerContact" maxlength="80"></div><div class="field"><label>${t('سياسة الاسترجاع المقترحة','Proposed return policy')}</label><textarea name="returnPolicy" maxlength="1000"></textarea><small class="helper">${t('لا يمكن للسياسة إلغاء حقوق المستهلك النظامية في عُمان.','The policy cannot override Oman statutory consumer rights.')}</small></div><button class="primary-button full" type="submit">${t('إرسال للمراجعة','Submit for review')}</button></form>`,root=>{$('#merchantApplyForm',root).addEventListener('submit',merchantApplySubmit);});
  }
  async function merchantApplySubmit(event){event.preventDefault();const data=Object.fromEntries(new FormData(event.currentTarget));try{const result=await api('/api/merchant/apply',{method:'POST',body:JSON.stringify(data)});closeSheet();toast(t(`تم إرسال الطلب ${result.id} للمراجعة`,`Application ${result.id} was submitted for review`));await load();}catch(error){toast(niceError(error));}}

  function quickProductSheet(){
    const d=state.dashboard;if(!d?.branches?.length)return toast(t('أضف فرعاً معتمداً أولاً','Add an approved branch first'));
    openSheet(t('منتج جديد','New product'),t('سريع، لكن بقيود الخادم','Fast, with server-enforced rules'),`<form id="productForm" class="form-grid"><div class="form-row"><div class="field"><label>${t('الاسم بالعربية','Arabic name')}</label><input name="nameAr" required maxlength="120"></div><div class="field"><label>${t('English name','الاسم بالإنجليزية')}</label><input name="nameEn" dir="ltr" required maxlength="120"></div></div><div class="form-row"><div class="field"><label>${t('الفئة','Category')}</label><select name="categoryId" required>${(state.bootstrap.categories||[]).map(c=>`<option value="${esc(c.id)}">${esc(c[state.lang==='ar'?'name_ar':'name_en'])}</option>`).join('')}</select></div><div class="field"><label>${t('الفرع','Branch')}</label><select name="branchId" required>${d.branches.map(b=>`<option value="${esc(b.id)}">${esc(b[state.lang==='ar'?'name_ar':'name_en'])}</option>`).join('')}</select></div></div><div class="form-row"><div class="field"><label>${t('السعر (ر.ع)','Price (OMR)')}</label><input name="price" type="number" min="0.100" max="2.000" step="0.001" required><small class="helper">${t('من 0.100 إلى 2.000 فقط','0.100 to 2.000 only')}</small></div><div class="field"><label>${t('الكمية','Quantity')}</label><input name="quantity" type="number" min="0" max="1000000" value="1" required></div></div><div class="field"><label>${t('وصف عربي مختصر','Short Arabic description')}</label><textarea name="descriptionAr" maxlength="500"></textarea></div><div class="field"><label>${t('Short English description','وصف إنجليزي مختصر')}</label><textarea name="descriptionEn" dir="ltr" maxlength="500"></textarea></div><button class="primary-button full">${t('حفظ المنتج','Save product')}</button></form>`,root=>{$('#productForm',root).addEventListener('submit',productSubmit);});
  }
  async function productSubmit(event){event.preventDefault();const data=Object.fromEntries(new FormData(event.currentTarget));try{await api('/api/merchant/product',{method:'POST',body:JSON.stringify(data)});closeSheet();toast(t('تم حفظ المنتج','Product saved'));await load();}catch(error){toast(niceError(error));}}

  async function stockSheet(){
    const branch=state.dashboard?.branches?.[0];if(!branch)return toast(t('لا يوجد فرع','No branch found'));
    let data;try{data=await api(`/api/merchant/stock?branch=${encodeURIComponent(branch.id)}`);}catch(error){return toast(niceError(error));}
    openSheet(t('تأكيد المخزون','Verify inventory'),t('المتاح أولاً، ثم الباقي','Priority items first'),`<form id="stockForm" class="form-grid">${data.items.length?data.items.map(item=>`<div class="option-card"><div class="stock-name"><b>${esc(item[state.lang==='ar'?'name_ar':'name_en'])}</b><small>${esc(item.availability)}</small></div><input class="stock-quantity" name="${esc(item.id)}" type="number" min="0" max="1000000" value="${item.quantity}"></div>`).join(''):`<div class="empty-state"><p>${t('لا توجد منتجات للمراجعة','No products to review')}</p></div>`}<button class="primary-button full" ${data.items.length?'':'disabled'}>${t('تأكيد الكميات','Confirm quantities')}</button></form>`,root=>{$('#stockForm',root).addEventListener('submit',event=>stockSubmit(event,branch.id,data.items));});
  }
  async function stockSubmit(event,branchId,items){event.preventDefault();const fd=new FormData(event.currentTarget);const changes=items.map(item=>({productId:item.id,quantity:Number(fd.get(item.id))}));try{await api('/api/merchant/stock',{method:'POST',body:JSON.stringify({branchId,changes})});closeSheet();toast(t('تم تأكيد المخزون','Inventory confirmed'));await load();}catch(error){toast(niceError(error));}}

  function locationSheet(){ const locations=(state.bootstrap.locations||[]).filter(l=>l.kind==='wilayat'||l.kind==='area');openSheet(t('أين تكتشف اليوم؟','Where are you discovering today?'),t('نظهر المناطق التي فيها متجر معتمد فقط','Only areas with an approved store are public'),`<div class="form-grid">${locations.map(l=>`<button class="option-card" data-location-id="${esc(l.id)}"><span>${l.kind==='wilayat'?'⌖':'•'}</span><div><b>${esc(l[state.lang==='ar'?'name_ar':'name_en'])}</b><small>${l.kind==='wilayat'?t('ولاية','Wilayat'):t('منطقة نشطة','Active area')}</small></div></button>`).join('')}</div>`); }
  function notificationSheet(){ const rows=state.bootstrap.notifications||[];openSheet(t('الإشعارات','Notifications'),t('المطلوب أولاً','Actions first'),rows.length?`<div class="form-grid">${rows.map(n=>`<article class="option-card"><span>${Number(n.requires_action)?'!':'♢'}</span><div><b>${esc(n[state.lang==='ar'?'title_ar':'title_en'])}</b><small>${esc(n[state.lang==='ar'?'body_ar':'body_en'])}</small></div></article>`).join('')}</div>`:`<div class="empty-state"><div class="empty-icon">♢</div><h3>${t('أنت على اطلاع','You are all caught up')}</h3><p>${t('الإجراءات الحقيقية المهمة فقط تظهر هنا.','Only real, important actions appear here.')}</p></div>`); }

  function purgeDemoSheet(){
    openSheet(t('حذف كل البيانات التجريبية','Delete all demo data'),t('إجراء إداري دائم ومسجّل','Permanent, audited admin action'),`<form id="purgeDemoForm" class="form-grid"><div class="warning-panel"><b>${t('لن تُحذف أي بيانات حقيقية','No real records will be deleted')}</b><p>${t('الحذف يستهدف فقط السجلات الموسومة Demo: المحلات، المنتجات، الباقات، الإعلانات، الفروع والحسابات التجريبية التابعة لها.','Only Demo-tagged stores, products, bundles, ads, branches and their demo accounts are targeted.')}</p></div><div class="field"><label>${t('اكتب العبارة التالية للتأكيد','Type this exact phrase to confirm')}</label><code dir="ltr">DELETE BISA DEMO</code><input name="confirmation" dir="ltr" autocomplete="off" required placeholder="DELETE BISA DEMO"></div><button class="danger-button full" type="submit">${t('حذف التجريبي نهائياً','Permanently delete demo data')}</button><button class="text-button full" type="button" data-close-sheet>${t('إلغاء','Cancel')}</button></form>`,root=>{$('#purgeDemoForm',root).addEventListener('submit',purgeDemoSubmit);});
  }
  async function purgeDemoSubmit(event){
    event.preventDefault(); const form=event.currentTarget,button=$('button[type="submit"]',form),confirmation=new FormData(form).get('confirmation');
    if(confirmation!=='DELETE BISA DEMO')return toast(t('عبارة التأكيد غير مطابقة','Confirmation phrase does not match'));
    button.disabled=true;
    try{
      if(STATIC_SHOWCASE){localStorage.setItem(STORAGE.demoPurged,'true');sessionStorage.removeItem('bisa.demo.cart');state.bootstrap=demoBootstrap();}
      else await api('/api/admin/demo-data/purge',{method:'POST',body:JSON.stringify({confirmation})});
      closeSheet();toast(t('حُذفت البيانات التجريبية فقط','Demo data removed'));await renderAdmin();
    }catch(error){toast(niceError(error));button.disabled=false;}
  }

  function demoAddToCart(payload,replaceCart=false){
    const rows=payload.kind==='bundle'?state.bootstrap.bundles:state.bootstrap.products;
    const item=(rows||[]).find(row=>row.id===payload.itemId); if(!item)throw Object.assign(new Error('item_not_found'),{code:'item_not_found'});
    const merchantId=item.merchant_id,branchId=payload.branchId||item.branch_id,cart=state.bootstrap.cart;
    if(cart?.items?.length&&cart.merchant_id!==merchantId&&!replaceCart)throw Object.assign(new Error('cross_store_cart_confirmation_required'),{code:'cross_store_cart_confirmation_required'});
    const price=Number(item.price||0).toFixed(3),name=item[state.lang==='ar'?(payload.kind==='bundle'?'title_ar':'name_ar'):(payload.kind==='bundle'?'title_en':'name_en')];
    const next=cart?.merchant_id===merchantId&&!replaceCart?cart:{merchant_id:merchantId,branch_id:branchId,items:[],subtotal:'0.000'};
    const found=next.items.find(row=>row.item_id===item.id&&row.item_kind===payload.kind);
    if(found)found.quantity+=1;else next.items.push({item_kind:payload.kind,item_id:item.id,snapshot_name:name,unitPrice:price,quantity:1});
    next.subtotal=next.items.reduce((sum,row)=>sum+Number(row.unitPrice)*Number(row.quantity),0).toFixed(3);
    sessionStorage.setItem('bisa.demo.cart',JSON.stringify(next));state.bootstrap.cart=next;return next;
  }

  async function addProduct(button){
    if(!state.account)return loginSheet('shopper');
    const kind=button.dataset.addBundle?'bundle':'product';
    const payload={kind,itemId:button.dataset.addBundle||button.dataset.addProduct,branchId:button.dataset.branch,quantity:1};
    try{state.bootstrap.cart=STATIC_SHOWCASE?demoAddToCart(payload):await api('/api/cart',{method:'POST',body:JSON.stringify(payload)});toast(kind==='bundle'?t('أُضيفت الباقة إلى السلة','Bundle added to cart'):t('أُضيف إلى السلة','Added to cart'));render();}
    catch(error){if(error.code==='cross_store_cart_confirmation_required')return confirmReplace(payload);toast(niceError(error));}
  }
  function confirmReplace(payload){openSheet(t('سلة من متجر آخر','Cart from another store'),t('نحافظ على وضوح التسليم','Keeping fulfillment clear'),`<div class="empty-state"><div class="empty-icon">⇄</div><h3>${t('استبدال محتوى السلة؟','Replace current cart?')}</h3><p>${t('بيسا يستخدم سلة متجر واحد. سيُزال محتوى السلة الحالية قبل إضافة هذا المنتج.','BISA uses a one-store cart. Current items will be removed before adding this product.')}</p><button class="primary-button full" data-confirm-replace>${t('نعم، استبدل السلة','Yes, replace cart')}</button><button class="text-button full" data-close-sheet>${t('إبقاء السلة الحالية','Keep current cart')}</button></div>`,root=>{$('[data-confirm-replace]',root).addEventListener('click',async()=>{try{state.bootstrap.cart=STATIC_SHOWCASE?demoAddToCart(payload,true):await api('/api/cart',{method:'POST',body:JSON.stringify({...payload,replaceCart:true})});closeSheet();toast(t('تم استبدال السلة','Cart replaced'));render();}catch(error){toast(niceError(error));}});});}
  async function checkout(){if(STATIC_SHOWCASE)return toast(t('هذه معاينة GitHub Pages. إرسال الطلب الحقيقي يحتاج تشغيل خادم BISA.','This is a GitHub Pages preview. Real orders require the BISA server.'));const mode=$('input[name="fulfillment"]:checked')?.value||'pickup';const key=crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random()}`;try{await api('/api/checkout',{method:'POST',headers:{'Idempotency-Key':key},body:JSON.stringify({idempotencyKey:key,fulfillmentMode:mode})});toast(t('أُرسل الطلب للمتجر','Order sent to store'));state.view='orders';await load();}catch(error){toast(niceError(error));}}
  async function decide(button){button.disabled=true;try{await api('/api/merchant/order',{method:'POST',body:JSON.stringify({orderId:button.dataset.orderId,decision:button.dataset.orderDecision})});toast(t('تم تحديث الطلب','Order updated'));await load();}catch(error){toast(niceError(error));button.disabled=false;}}
  async function decideApplication(button){button.disabled=true;const note=button.dataset.adminDecision==='approve'?'':prompt(t('ملاحظة القرار','Decision note'))||'';try{await api('/api/admin/merchant-application',{method:'POST',body:JSON.stringify({applicationId:button.dataset.adminApplication,decision:button.dataset.adminDecision,note})});toast(t('تم حفظ القرار في سجل التدقيق','Decision saved to the audit log'));renderAdmin();}catch(error){toast(niceError(error));button.disabled=false;}}
  async function logout(){try{await api('/api/auth/logout',{method:'POST',body:'{}'});}catch{}clearAuth();closeSheet();state.view='home';toast(t('تم تسجيل الخروج','Signed out'));await load();}

  document.addEventListener('click',event=>{
    const button=event.target.closest('button,[data-close-sheet]');if(!button)return;
    if(button.matches('[data-close-sheet]'))return closeSheet();
    if(button.dataset.view){state.view=button.dataset.view;history.replaceState({},'',`?view=${state.view}`);render();scrollTo({top:0,behavior:'smooth'});}
    else if(button.dataset.merchantView){state.merchantView=button.dataset.merchantView;render();scrollTo({top:0,behavior:'smooth'});}
    else if(button.dataset.go){state.view=button.dataset.go;render();scrollTo({top:0,behavior:'smooth'});}
    else if(button.dataset.merchantGo){state.merchantView=button.dataset.merchantGo;render();}
    else if(button.dataset.category!==undefined){state.filters.category=button.dataset.category;state.view='explore';render();}
    else if(button.dataset.display){state.filters.display=button.dataset.display;render();}
    else if(button.dataset.addProduct||button.dataset.addBundle)addProduct(button);
    else if(button.dataset.loginRole)loginSheet(button.dataset.loginRole);
    else if(button.dataset.open==='merchantIntro'||button.dataset.open==='merchantApply')merchantApplySheet();
    else if(button.dataset.open==='quickProduct')quickProductSheet();
    else if(button.dataset.open==='stockCheck')stockSheet();
    else if(button.dataset.open==='purgeDemo')purgeDemoSheet();
    else if(button.dataset.open==='filters')toast(t('اختر الفئة أو استخدم البحث','Choose a category or use search'));
    else if(button.hasAttribute('data-checkout'))checkout();
    else if(button.dataset.orderDecision)decide(button);
    else if(button.dataset.adminDecision)decideApplication(button);
    else if(button.hasAttribute('data-logout'))logout();
    else if(button.hasAttribute('data-toggle-language'))toggleLanguage();
    else if(button.dataset.locationId){const item=(state.bootstrap.locations||[]).find(l=>l.id===button.dataset.locationId);if(item){state.location=item;localStorage.setItem(STORAGE.location,JSON.stringify(item));closeSheet();render();}}
  });
  $('#brandButton').addEventListener('click',()=>{state.view='home';state.merchantView='today';render();});
  $('#locationButton').addEventListener('click',locationSheet); $('#notificationButton').addEventListener('click',notificationSheet);
  $('#languageButton').addEventListener('click',toggleLanguage);
  function toggleLanguage(){state.lang=state.lang==='ar'?'en':'ar';localStorage.setItem(STORAGE.language,state.lang);render();}
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('#sheetRoot').hidden)closeSheet();});
  window.addEventListener('popstate',()=>{state.view=new URLSearchParams(location.search).get('view')||'home';render();});
  navigator.serviceWorker?.addEventListener('message',event=>{if(event.data?.type==='bisa:notification'){load({quiet:true});}});

  if('serviceWorker' in navigator && location.protocol.startsWith('http')) navigator.serviceWorker.register('service-worker.js').catch(()=>{});
  load();
})();
