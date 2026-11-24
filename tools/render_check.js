// Kør alle Varmemodel-kort fra en dashboard-dump mod rigtige tilstande (uden browser) og meld fejl.
// Brug: node tools/render_check.js <hassdump.json> [kort.js]
// hassdump.json: [get_states, entity_registry/list_for_display, device_registry/list, lovelace/config] som WS-svar.
'use strict';
const fs = require('fs');
const [dumpPath, jsPath = __dirname + '/../custom_components/varmemodel/frontend/varmemodel-cards.js'] = process.argv.slice(2);
const dump = JSON.parse(fs.readFileSync(dumpPath, 'utf8')).map((x) => x.result);
const [states, ereg, dreg, dash] = dump;

class FakeEl { constructor() { this.dataset = {}; } attachShadow() { this.shadowRoot = { innerHTML: '', addEventListener() {}, getElementById() { return null; }, querySelectorAll() { return []; } }; return this.shadowRoot; }
  dispatchEvent() {} addEventListener() {} get clientWidth() { return global.__W || 1400; } }
const defs = {};
global.HTMLElement = FakeEl;
global.ResizeObserver = class { observe() {} disconnect() {} };
global.window = { customCards: [], addEventListener() {}, dispatchEvent() {} };
global.customElements = { get: (t) => defs[t], define: (t, c) => { defs[t] = c; } };
global.CustomEvent = class { constructor(t, d) { this.type = t; this.detail = d; } };
global.history = { pushState() {} };
eval(fs.readFileSync(jsPath, 'utf8'));

const hass = {
  states: Object.fromEntries(states.map((s) => [s.entity_id, s])),
  entities: Object.fromEntries(ereg.entities.map((e) => [e.ei, { entity_id: e.ei, platform: e.pl, device_id: e.di, translation_key: e.tk }])),
  devices: Object.fromEntries(dreg.map((d) => [d.id, d])),
  callWS: () => Promise.resolve({}), callService: () => Promise.resolve(),
  language: 'da',
};

const cards = [];
const walk = (o, where) => {
  if (Array.isArray(o)) o.forEach((x) => walk(x, where));
  else if (o && typeof o === 'object') {
    if (typeof o.type === 'string' && o.type.startsWith('custom:varmemodel')) cards.push([where, o]);
    for (const v of Object.values(o)) walk(v, where);
  }
};
for (const v of dash.views) walk(v, v.title);

// Dashboardet på ét klik (strategy): dens kort tegnes også
const strat = defs['ll-strategy-dashboard-varmemodel'];
const stratViews = [];
if (strat) { strat.generate({}, hass).then((d) => stratViews.push(...d.views)); }
let bad = 0;
(async () => {
await Promise.resolve(); await Promise.resolve();
for (const v of stratViews) walk(v, `strategy: ${v.title}`);
for (const W of [1400, 420]) {
  global.__W = W;
  for (const [where, cfg] of cards) {
    const C = defs[cfg.type.slice(7)];
    if (!C) { console.log(`MANGLER ${cfg.type}`); bad++; continue; }
    const el = new C(); el._tall = W < 700; el.setConfig(cfg); el.hass = hass; el._draw();
    for (const k of [0, 3]) {
      if (el._setK) { el._k = k; el._draw(); }
      const html = el.shadowRoot ? el.shadowRoot.innerHTML : '';
      const m = html.match(/Fejl i kortet: ([^<]*)/);
      if (m) { console.log(`FEJL [${W}px, k=${k}] ${where} · ${cfg.type}: ${m[1]}`); bad++; }
    }
  }
}
console.log(bad ? `${bad} fejl` : `alle ${cards.length} kort tegnet uden fejl (bred og smal, inkl. ${stratViews.length} strategy-faner)`);
process.exit(bad ? 1 : 0);
})();
