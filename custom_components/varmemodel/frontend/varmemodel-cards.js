/* Varmemodel-kort til Home Assistant.
 *
 * Kortene finder selv Varmemodels entiteter (platform "varmemodel") og enhedstræet
 * Hus → varmekilde → rum. Alt husspecifikt står i kortets opsætning (se README):
 * buffer, kortslutning (T-stykke), cirkulationspumpe og ekstra følere.
 *
 *   custom:varmemodel-anlaeg-card   anlægget: nu, planen og afvigelser
 *   custom:varmemodel-rum-card      rummene: målt, forudsagt og om forudsigelsen holdt
 *   custom:varmemodel-kilde-card    varmekildernes tal: effektivitet, bygning, anlæg
 *   custom:varmemodel-tiltag-card   energitiltag: afkøling, vaner, vind og før/efter
 *   custom:varmemodel-hus-card      Varmemodels status, planen og indstillingerne
 *
 * Pålidelighed: sikre tal vises som de er, foreløbige med »≈«, usikre nedtonet med »≈«,
 * manglende som »–«. Tryk på et tal for detaljer.
 */
(() => {
  'use strict';
  const VERSION = '0.16.5';

  // ------------------------------------------------------------------ hjælpere
  const num = (v) => { const x = parseFloat(v); return Number.isFinite(x) ? x : null; };
  const fmt = (v, d = 1) => (v === null || v === undefined || !Number.isFinite(+v) ? '–' : (+v).toFixed(d).replace('.', ','));
  const sgn = (v, d = 1) => (v === null || v === undefined ? '–' : (v > 0 ? '+' : v < 0 ? '−' : '±') + fmt(Math.abs(v), d));
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const st = (h, e) => (e && h.states[e]) || null;
  // Følere der falder kortvarigt ud: vis seneste kendte værdi i op til 30 min. i stedet for »–«
  const LASTV = {};
  const sv = (h, e) => {
    const s = st(h, e); const v = s ? num(s.state) : null;
    if (v !== null) { LASTV[e] = [v, Date.now()]; return v; }
    const l = LASTV[e]; return l && Date.now() - l[1] < 1800e3 ? l[0] : null;
  };
  const at = (h, e) => (st(h, e) || {}).attributes || {};
  const isOn = (h, e) => (st(h, e) || {}).state === 'on';
  const hh = (n) => String(n).padStart(2, '0');
  const when = (iso) => {
    const d = new Date(iso); if (isNaN(d)) return '–';
    const a = new Date(d); a.setHours(0, 0, 0, 0); const b = new Date(); b.setHours(0, 0, 0, 0);
    const k = Math.round((a - b) / 864e5);
    const day = { '-1': 'i går ', 0: '', 1: 'i morgen ', 2: 'i overmorgen ' }[k];
    return (day ?? d.toLocaleDateString('da-DK', { day: 'numeric', month: 'numeric' }) + ' ') + 'kl. ' + hh(d.getHours());
  };
  const whenM = (iso) => { const d = new Date(iso); return isNaN(d) ? '–' : `${when(iso)}:${hh(d.getMinutes())}`; };
  const kW = (h, e) => { const v = sv(h, e); if (v === null) return null; return String(at(h, e).unit_of_measurement || '').toLowerCase() === 'w' ? v / 1000 : v; };
  const GR = '#2e9e5b', YE = '#d29b12', RE = '#d6453d', BL = '#3b82c4', OR = '#f2711c', ORT = '#e8890c', PU = '#8b5cf6';
  const mixCol = (v) => (v === null ? null : v > 1.5 ? RE : v > 0.5 ? YE : v < -0.5 ? BL : GR);

  // ------------------------------------------------------------------ Varmemodels entiteter
  let cache = { ents: null, devs: null, vm: null };
  function discover(h) {
    if (cache.ents === h.entities && cache.devs === h.devices) return cache.vm;
    const ents = Object.values(h.entities || {}).filter((e) => e.platform === 'varmemodel');
    const status = ents.find((e) => e.translation_key === 'status');
    let vm = null;
    if (status) {
      const tk = (dev, key) => (ents.find((e) => e.device_id === dev && e.translation_key === key) || {}).entity_id;
      const devs = Object.values(h.devices || {});
      const name = (d) => d.name_by_user || d.name || '';
      const sources = devs.filter((d) => d.via_device_id === status.device_id && ents.some((e) => e.device_id === d.id)).map((d) => {
        const plan = tk(d.id, 'plan_now');
        const rooms = devs.filter((r) => r.via_device_id === d.id)
          .map((r) => ({ id: r.id, name: name(r), model: tk(r.id, 'room_device') || tk(r.id, 'room'), heat: tk(r.id, 'room_heat_device') || tk(r.id, 'room_heat') }))
          .filter((r) => r.model);
        // Luft-luft har også en plan (fra 0.6): typen afgøres af enhedens model, ellers af fremløbsplanen
        const hydro = d.model ? d.model !== 'Luft-luft' : !!tk(d.id, 'plan_flow');
        return { id: d.id, name: name(d), kind: hydro ? 'hydronic' : 'air_air', plan, rooms, e: (k) => tk(d.id, k) };
      });
      sources.forEach((s) => s.rooms.forEach((r) => { r.src = s; }));
      const hyd = sources.find((s) => s.kind === 'hydronic');
      vm = { status: status.entity_id, e: (k) => tk(status.device_id, k), sources, rooms: sources.flatMap((s) => s.rooms), plan: hyd ? hyd.plan : null };
    }
    cache = { ents: h.entities, devs: h.devices, vm };
    return vm;
  }
  const sensorsOf = (h, s) => at(h, s.kind === 'hydronic' ? s.plan : s.e('el_yesterday'))['følere'] || {};

  // Rummets præcision over 6 timer: den afprøvede (modellen kørt frem med målt tilførsel), ellers fittets
  const P6 = (a) => (a['præcision_6_timer_afprøvet'] != null ? Math.max(a['præcision_6_timer_afprøvet'], a['præcision_6_timer'] ?? 0) : a['præcision_6_timer']);
  // Pålidelighed for et tal fra Varmemodel
  function rel(s) {
    if (!s || num(s.state) === null) return 'mangler';
    const a = s.attributes || {};
    const r2 = a.forklaringsgrad, p6 = P6(a);
    if ((r2 != null && r2 < 0.3) || (p6 != null && p6 > 0.6) || a.kilde === 'prior') return 'usikker';
    const lim = String(a['begrænsning'] ?? 'ok');
    if (lim.startsWith('ny model') || (a.blokke != null && a.blokke < 100) || (a['målinger'] != null && a['målinger'] < 20)) return 'forel';
    return 'sikker';
  }
  const RELTXT = { sikker: 'sikker', forel: 'foreløbig', usikker: 'usikker', mangler: 'mangler data' };
  // Tal fra en entitet: pålidelighed, pil når vagten har holdt tallet tilbage, tryk = detaljer
  function V(h, e, d = 1, unit = '') {
    const s = st(h, e); const r = rel(s);
    if (r === 'mangler') {
      const why = s ? (s.attributes.note || s.attributes['begrænsning'] || 'for lidt data') : 'ikke fundet';
      return `<span class="v mangler" data-e="${esc(e || '')}" title="${esc(why)}">–</span>`;
    }
    const a = s.attributes; const v = num(s.state);
    const lim = String(a['begrænsning'] ?? 'ok'); const bv = num(a['beregnet_værdi']);
    const arrow = bv !== null && lim !== 'ok' && !lim.startsWith('ny model') && Math.abs(bv - v) > 5e-4 ? (bv > v ? ' ↗' : ' ↘') : '';
    return `<span class="v ${r}" data-e="${esc(e)}" title="${RELTXT[r]}">${r !== 'sikker' ? '≈' : ''}${fmt(v, d)}${unit ? ' ' + unit : ''}${arrow}</span>`;
  }
  // Tal fra en attribut, med entitetens pålidelighed
  function A(h, e, v, d = 1, unit = '') {
    const r = rel(st(h, e));
    if (v === null || v === undefined || !Number.isFinite(+v)) return `<span class="v mangler" data-e="${esc(e || '')}">–</span>`;
    const c = r === 'mangler' ? '' : r;
    return `<span class="v ${c}" data-e="${esc(e)}" title="${RELTXT[r]}">${c && c !== 'sikker' ? '≈' : ''}${fmt(v, d)}${unit ? ' ' + unit : ''}</span>`;
  }
  // Almindelig måling (ingen model): tryk = historik
  const M = (h, e, d = 1, unit = '', cls = '') => `<span class="v ${cls}" data-e="${esc(e || '')}">${fmt(sv(h, e), d)}${unit ? ' ' + unit : ''}</span>`;

  // Et rum: målt, forudsagt for 6 timer siden (ramt/over/under) og de næste timer
  function room(h, vm, r) {
    const a = at(h, r.model); const pa = at(h, vm.plan);
    const tE = a['temperaturføler']; let t = sv(h, tE);
    // Føleren har været væk i over 30 min.: planen bruger rummets forudsigelse – vis den med »≈«
    const est = t === null ? ((pa.rooms_estimated || {})[r.name] || null) : null;
    if (est) t = num(est['forudsagt']);
    const fc = (pa.rooms_forecast || {})[r.name] || null; const pl = pa.plan || [];
    const chk = (pa.rooms_check || {})[r.name] || null;
    let low = null, lowT = null;
    if (fc && fc.length && pl.length >= fc.length) { let k = 0; fc.forEach((v, i) => { if (v < fc[k]) k = i; }); low = fc[k]; lowT = pl[k].t; }
    const status = chk ? chk.status || (Math.abs(chk['afvigelse']) <= 0.2 ? 'ramt' : chk['afvigelse'] > 0 ? 'over' : 'under') : null;
    const cls = status === 'over' ? 'orange' : status === 'under' ? 'blue' : '';
    const valve = a.telestat || null;
    const vs = valve === 'altid åben' ? 'on' : valve ? (st(h, valve) || {}).state : null;
    const passive = a.i_plan === false && !valve && r.src.kind === 'hydronic';
    return { r, a, tE, t, est, fc, f3: fc && fc.length > 2 ? fc[2] : null, f6: fc && fc.length > 5 ? fc[5] : null, low, lowT, chk, status, cls, valve, vs, passive };
  }
  // Rummets temperatur nu som tekst: målt, eller »≈« forudsagt, når føleren er væk
  const tTxt = (d) => (d.t === null ? '–' : `${d.est ? '≈' : ''}${fmt(d.t)} °C`);
  const estTip = (d) => (d.est ? `forudsagt – føleren har ikke målt siden ${whenM(d.est['sidste_måling'])}; planen bruger forudsigelsen` : '');
  const statusTxt = (d) => (d.est ? 'uden måling' : !d.chk ? (d.fc ? 'tjekkes efter 6 t' : 'ingen forudsigelse') : d.status === 'ramt' ? 'ramt' : `${d.status} ${sgn(d.chk['afvigelse'])}`);
  // Hvor meget et rum varmer eller køler sine naborum lige nu (fx en kælder under et køkken)
  function pulls(h, vm, r, t) {
    let best = null;
    for (const o of vm.rooms) {
      if (o === r) continue;
      const nb = (at(h, o.model).nabozoner || {})[r.name]; if (!nb) continue;
      const to = sv(h, at(h, o.model)['temperaturføler']); if (to === null || t === null) continue;
      const eff = nb.per_h_k * (t - to);
      if (!best || Math.abs(eff) > Math.abs(best.eff)) best = { name: o.name, eff };
    }
    return best;
  }

  // ------------------------------------------------------------------ fælles kort
  const CSS = `
  :host{display:block}
  ha-card{padding:12px 14px;overflow:hidden}
  .h{font-size:16px;font-weight:600;margin:4px 0 8px}
  .h2{font-size:13px;font-weight:600;margin:14px 0 6px;color:var(--secondary-text-color);text-transform:uppercase;letter-spacing:.04em}
  .mut{color:var(--secondary-text-color)}.sm{font-size:12px}
  .v{cursor:pointer;white-space:nowrap}.v:hover{text-decoration:underline dotted}
  .v.usikker{opacity:.5}.v.mangler{color:var(--secondary-text-color)}
  .wrap{overflow-x:auto}
  table{border-collapse:collapse;width:100%;font-size:14px}
  th{font-weight:500;color:var(--secondary-text-color);text-align:left;padding:4px 6px;border-bottom:1px solid var(--divider-color);white-space:nowrap}
  td{padding:6px;border-bottom:1px solid var(--divider-color);vertical-align:top;white-space:nowrap}
  .n{text-align:right}
  .blue{color:${BL}}.orange{color:${ORT}}.red{color:${RE}}.green{color:${GR}}.amber{color:${YE}}
  .flag{color:#fff;background:${RE};border-radius:8px;padding:0 6px;font-weight:700;margin-left:4px;cursor:pointer}
  .leg{font-size:12px;color:var(--secondary-text-color);margin-top:10px;line-height:1.6}
  .grid{display:grid;grid-template-columns:1fr auto;gap:5px 14px;font-size:14px;align-items:baseline}
  .grid>div:nth-child(odd){color:var(--secondary-text-color)}
  button.lnk{font:inherit;color:var(--primary-color);background:none;border:0;padding:0;cursor:pointer}
  .bars text{fill:var(--secondary-text-color);font-size:11px}
  `;
  const LEG = '<div class="leg">≈ foreløbig · <span style="opacity:.5">≈ usikker</span> · – mangler data · ↗/↘ holdt tilbage af vagten · '
    + '<span class="orange">over</span>/<span class="blue">under</span> forudsigelsen fra 6 timer før · <span class="flag">!</span> afviger mere end dobbelt så meget som normalt · tryk på et tal for detaljer</div>';

  class Base extends HTMLElement {
    setConfig(c) { this._c = c || {}; this._open = this._open || new Set(); if (this._h) this._draw(); }
    set hass(h) {
      this._h = h;
      const t = Date.now();
      if (t - (this._last || 0) > 2500) { this._last = t; this._draw(); }
      else if (!this._tm) this._tm = setTimeout(() => { this._tm = null; this._last = Date.now(); this._draw(); }, 2500);
    }
    get hass() { return this._h; }
    getCardSize() { return 8; }
    getGridOptions() { return { columns: 'full', min_columns: 6 }; }
    _draw() {
      if (!this._h || !this._c) return;
      if (!this.shadowRoot) {
        const r = this.attachShadow({ mode: 'open' });
        r.addEventListener('click', (ev) => {
          const path = ev.composedPath();
          const act = path.find((x) => x.dataset && x.dataset.act);
          if (act) { this._h.callService('homeassistant', 'toggle', { entity_id: act.dataset.act }); return; }
          const kk = path.find((x) => x.dataset && x.dataset.k !== undefined);
          if (kk && this._setK) { this._setK(kk.dataset.k); return; }
          const nav = path.find((x) => x.dataset && x.dataset.nav);
          if (nav) { history.pushState(null, '', nav.dataset.nav); window.dispatchEvent(new CustomEvent('location-changed')); return; }
          const t = path.find((x) => x.dataset && x.dataset.e);
          if (t) { ev.stopPropagation(); this.dispatchEvent(new CustomEvent('hass-more-info', { detail: { entityId: t.dataset.e }, bubbles: true, composed: true })); }
        });
        // Indstillinger: dropdowns, tal og tider skriver straks til Varmemodels entiteter
        r.addEventListener('change', (ev) => {
          const el = ev.target; if (!el || !el.dataset || !el.dataset.svc) return;
          const [dom, svc] = el.dataset.svc.split('.'); const eid = el.dataset.eid; let data = { entity_id: eid };
          if (svc === 'select_option') data.option = el.value;
          else if (svc === 'set_value' && dom === 'time') data.time = el.value.length === 5 ? el.value + ':00' : el.value;
          else if (svc === 'set_value') data.value = +el.value;
          this._h.callService(dom, svc, data); el.blur();
        });
        r.addEventListener('toggle', (ev) => { const d = ev.target; if (d && d.id) { if (d.open) this._open.add(d.id); else this._open.delete(d.id); } }, true);
      }
      // Tegn ikke om, mens en dropdown eller et felt er i brug (ellers lukker den)
      const ae = this.shadowRoot.activeElement;
      if (ae && ['SELECT', 'INPUT'].includes(ae.tagName)) { clearTimeout(this._tmE); this._tmE = setTimeout(() => this._draw(), 1500); return; }
      const vm = discover(this._h);
      let body;
      try { body = vm ? this.body(this._h, vm) : '<div class="mut">Varmemodel er ikke sat op endnu.</div>'; }
      catch (e) { body = `<div class="red">Fejl i kortet: ${esc(e && e.message)}</div>`; }
      this.shadowRoot.innerHTML = `<style>${CSS}${this.css || ''}</style><ha-card>${this._c.title ? `<div class="h">${esc(this._c.title)}</div>` : ''}${body}</ha-card>`;
      for (const id of this._open) { const d = this.shadowRoot.getElementById(id); if (d) d.open = true; }
    }
  }


  // ------------------------------------------------------------------ plantegning (heatmap)
  // Rummene tegnes efter din plantegning (cm) og farves efter temperaturen i forhold til rummets gulv og loft:
  // blå under gulvet, lyseblå tæt på, grøn godt, gul tæt på loftet, rød over. Knapper viser forudsigelsen de næste timer.
  const TCOL = (t, lo, hi) => {
    if (t === null) return '#e0e0e0';
    const fl = lo ?? 19.5, cap = hi ?? fl + 3;
    if (t < fl) return '#7fb2e5';
    if (t < fl + 0.5) return '#b9dcf2';
    if (t <= cap - 0.5) return '#a8dbb5';
    if (t <= cap) return '#f6d98b';
    return '#ef9a8f';
  };
  // Glidende farve: blå under gulvet → grøn i komfort → rav ved loftet → rød over
  const HCOL = (t, lo, hi) => {
    if (t === null) return '#8a9198';
    const fl = lo ?? 19.5, cap = hi ?? fl + 3;
    const stops = [[fl - 1, 212], [fl, 200], [fl + 0.5, 150], [cap - 0.5, 140], [cap, 42], [cap + 1, 4]];
    let hue = t <= stops[0][0] ? stops[0][1] : stops[stops.length - 1][1];
    for (let i = 0; i < stops.length - 1; i++) { const [a, ha] = stops[i], [b, hb] = stops[i + 1]; if (t >= a && t <= b) { hue = ha + ((t - a) / (b - a || 1)) * (hb - ha); break; } }
    return `hsl(${Math.round(hue)} 62% 52%)`;
  };
  const pts = (x) => (x.points || (x.rect ? [[x.rect[0], x.rect[1]], [x.rect[0] + x.rect[2], x.rect[1]], [x.rect[0] + x.rect[2], x.rect[1] + x.rect[3]], [x.rect[0], x.rect[1] + x.rect[3]]] : []));
  const mid = (p) => [p.reduce((a, q) => a + q[0], 0) / (p.length || 1), p.reduce((a, q) => a + q[1], 0) / (p.length || 1)];

  // Opvarmning før rød: tekst ud fra planens preload_window
  function preText(h, s, short) {
    const w = at(h, s.plan).preload_window; if (!w || !w.fra) return '';
    const u = s.kind === 'hydronic' ? 'kWh varme' : 'kWh el'; const k = (t) => hh(new Date(t).getHours());
    const now = Date.now(); const on = Date.parse(w.fra) <= now && now < Date.parse(w.til);
    const t = `opvarmning før rød kl. ${k(w.fra)}–${k(w.til)}${on ? ' (nu)' : ''}` + (w.blokering_fra ? ` · rød ${k(w.blokering_fra)}–${k(w.blokering_til)}` : '');
    return short ? t : `${t} (${fmt(w.kwh)} ${u})`;
  }

  // Indstillinger for en Varmemodel-enhed som rigtige kontroller (dropdown, tal, tid, kontakt), grupperet
  const SET_GROUPS = [
    ['Styring', ['air_control', 'air_boost', 'air_start', 'hydro_control', 'flow_control']],
    ['Fremløb', ['soft_start']],
    ['Støj: maks. blæser', ['air_fan_day', 'air_fan_night', 'air_night_start', 'air_night_end']],
    ['Lameller', ['air_swing_v', 'air_swing_h']],
  ];
  // Kortets egne navne (uafhængigt af HA's systemsprog)
  const SET_NAMES = {
    air_control: 'Varmemodel styrer', air_boost: 'Jeg fryser', flow_control: 'Varmemodel styrer fremløb', hydro_control: 'Varmemodel styrer varmepumpen', air_fan_day: 'Maks. blæser dag', air_fan_night: 'Maks. blæser nat',
    air_night_start: 'Nat begynder', air_night_end: 'Nat slutter', air_swing_v: 'Lameller op/ned', air_swing_h: 'Lameller side',
    air_start: 'Opstart', soft_start: 'Fremløb efter start',
  };
  // Valgenes navne på kortet (værdien, der gemmes, er uændret)
  const OPT_NAMES = { fra: 'fra (planens fremløb)', til: 'blød start', test: 'test: hver anden blød', lavest: 'søg laveste fremløb',
    rolig: 'rolig (30–60 min. længere)', hurtig: 'hurtig' };
  function settingsPanel(h, src, title = 'Indstillinger') {
    let o = '';
    for (const [g, keys] of SET_GROUPS) {
      const rows = keys.map((k) => {
        const eid = src.e(k); const s0 = st(h, eid); if (!s0) return '';
        const a = s0.attributes || {}; const dom = eid.split('.')[0]; const name = SET_NAMES[k] || String(a.friendly_name || k).replace(src.name, '').trim();
        let ctl = '';
        if (dom === 'select') ctl = `<select data-svc="select.select_option" data-eid="${esc(eid)}">${(a.options || []).map((x) => `<option value="${esc(x)}"${x === s0.state ? ' selected' : ''}>${esc(OPT_NAMES[x] || x)}</option>`).join('')}</select>`;
        else if (dom === 'number') ctl = `<input type="number" data-svc="number.set_value" data-eid="${esc(eid)}" value="${esc(s0.state)}" min="${a.min}" max="${a.max}" step="${a.step}"><span class="mut sm"> ${esc(a.unit_of_measurement || '')}</span>`;
        else if (dom === 'time') ctl = `<input type="time" data-svc="time.set_value" data-eid="${esc(eid)}" value="${esc(String(s0.state).slice(0, 5))}">`;
        else if (dom === 'switch') ctl = `<button class="tg2${s0.state === 'on' ? ' on' : ''}" data-act="${esc(eid)}">${s0.state === 'on' ? 'til' : 'fra'}</button>`;
        return `<div class="sr"><span>${esc(name)}</span><span>${ctl}</span></div>`;
      }).join('');
      if (rows) o += `<div class="sg"><div class="sgh">${esc(g)}</div>${rows}</div>`;
    }
    return o ? `<div class="b"><div class="pvh">${esc(title)}<span class="mut sm"> · gemmes i Varmemodel</span></div>${o}</div>` : '';
  }

  // Én etage som heatmap (cm-koordinater). o.unit: varmepumpens tilstand nu ('heat' | 'idle' | 'defrost' | 'off'),
  // så luftpilene strømmer, når den varmer. o.detail: gulv (klik = gulv-hjælperen) og forudsigelse i hvert rum.
  function floorSvg(h, vm, f, k, o = {}) {
    const W = f.width || 700, H = f.height || 500;
    // Samme glidende farveskala som rumkortene, dæmpet fyld, så tekst i temaets farve kan læses (lyst og mørkt tema)
    const op = f.image ? (f.room_opacity ?? 0.6) : 0.45;
    let g = f.image ? `<image href="${esc(f.image)}" x="0" y="0" width="${W}" height="${H}" preserveAspectRatio="none" opacity="${f.image_opacity ?? 1}"/>` : '';
    const labels = [];
    for (const rr of f.rooms || []) {
      const p = pts(rr); if (!p.length) continue;
      const poly = p.map((q) => q.join(',')).join(' ');
      const r = vm.rooms.find((x) => x.name === (rr.room || rr.name));
      if (!r) { g += `<polygon points="${poly}" fill="none" stroke="var(--divider-color,#888)" stroke-width="3" stroke-dasharray="10 8"/>`; const [cx, cy] = mid(p); labels.push(`<text x="${cx}" y="${cy}" class="pn" text-anchor="middle">${esc(rr.label || rr.name || '')}</text>`); continue; }
      const d = room(h, vm, r); const a = d.a;
      const t = k === 0 ? d.t : (d.fc && d.fc.length >= k ? d.fc[k - 1] : null);
      const col = HCOL(t, num(a.gulv), num(a.loft));
      g += `<polygon points="${poly}" fill="${col}" fill-opacity="${op}" stroke="${col}" stroke-width="4" data-e="${esc(d.tE || r.model)}"/>`;
      if (rr.sensor) g += `<circle cx="${rr.sensor[0]}" cy="${rr.sensor[1]}" r="10" fill="#d9534f" stroke="#fff" stroke-width="3"><title>føler</title></circle>`;
      if (rr.room) continue;  // del af et andet rum: kun farven
      const [cx, cy] = rr.label_at || mid(p);
      const est = k === 0 && d.est;
      let l = `<text x="${cx}" y="${cy - 8}" class="pn" text-anchor="middle" data-e="${esc(d.tE || r.model)}">${esc(rr.label || r.name)}</text>`
        + `<text x="${cx}" y="${cy + 30}" class="pt" text-anchor="middle" data-e="${esc(d.tE || r.model)}">${t === null ? '–' : `${est ? '≈' : ''}${fmt(t)} °C`}</text>`;
      if (o.detail && !rr.compact) {
        const gE = a.min_entitet || r.model; const gv = a.min_entitet ? sv(h, a.min_entitet) : num(a.gulv);
        if (a.i_plan !== false && gv !== null) l += `<text x="${cx}" y="${cy + 62}" class="ps gl" text-anchor="middle" data-e="${esc(gE)}">gulv ${fmt(gv)} ✎</text>`;
        if (d.fc && d.fc.length > 5) l += `<text x="${cx}" y="${cy + 92}" class="ps" text-anchor="middle">3 t ${fmt(d.fc[2])} · 6 t ${fmt(d.fc[5])}</text>`;
      } else {
        const lim = [num(a.gulv) !== null ? `gulv ${fmt(a.gulv)}` : '', num(a.loft) !== null ? `loft ${fmt(a.loft)}` : ''].filter(Boolean).join(' · ');
        if (lim && !rr.compact) l += `<text x="${cx}" y="${cy + 58}" class="ps" text-anchor="middle">${lim}</text>`;
      }
      labels.push(l);
    }
    for (const dd of f.doors || []) g += `<line x1="${dd[0]}" y1="${dd[1]}" x2="${dd[2]}" y2="${dd[3]}" stroke="#fff" stroke-width="9"/>`;
    const UC = { heat: OR, defrost: BL, idle: '#8a9198', off: '#8a9198' };
    for (const u of f.units || []) {
      const w = u.w || 90, hh2 = u.h || 26;
      const live = o.unit && u.entity && o.unitEntity === u.entity ? o.unit : null;
      const fill = live ? UC[live] || '#5b9bd5' : '#5b9bd5';
      g += `<rect x="${u.x - w / 2}" y="${u.y - hh2 / 2}" width="${w}" height="${hh2}" rx="4" fill="${fill}" stroke="#2c6aa0" stroke-width="2"${u.entity ? ` data-e="${esc(u.entity)}"` : ''}/>`
        + `<text x="${u.x}" y="${u.y + 6}" class="pu" text-anchor="middle"${u.entity ? ` data-e="${esc(u.entity)}"` : ''}>${esc(u.name || '')}</text>`;
      const sc = live === 'heat' ? OR : live === 'defrost' ? BL : live ? '#9aa0a6' : '#2c6aa0';
      for (const path of u.paths || (u.to ? [u.to] : [])) {
        if (path.length) g += `<polyline points="${[[u.x, u.y], ...path].map((q) => q.join(',')).join(' ')}" fill="none" stroke="${sc}" stroke-width="5" stroke-dasharray="14,8" class="${live === 'heat' ? 'flow' : ''}" marker-end="url(#vmpil)"/>`;
      }
    }
    return `<svg viewBox="-10 -10 ${W + 20} ${H + 20}" class="psvg">`
      + '<defs><marker id="vmpil" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0 L10 5 L0 10 z" fill="context-stroke"/></marker></defs>'
      + `${g}${labels.join('')}</svg>`;
  }
  const PCSS = `.pl{display:flex;flex-wrap:wrap;gap:14px}.pf{flex:1 1 320px;min-width:280px}.psvg{width:100%;height:auto;display:block}
        .pn{font-size:30px;fill:var(--primary-text-color)}.pt{font-size:40px;font-weight:700;fill:var(--primary-text-color)}.ps{font-size:22px;fill:var(--secondary-text-color)}.pu{font-size:18px;fill:#fff}
        .bleg{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.grad{width:120px;height:10px;border-radius:5px;background:linear-gradient(90deg,hsl(212 62% 52%),hsl(150 62% 52%),hsl(140 62% 52%),hsl(42 62% 52%),hsl(4 62% 52%))}
        .gl{fill:#1c5fa8;cursor:pointer;text-decoration:underline dotted}.psvg [data-e]{cursor:pointer}
        .flow{animation:vmflow 1.1s linear infinite}@keyframes vmflow{to{stroke-dashoffset:-44}}
        .pill{display:inline-block;padding:2px 10px;border-radius:12px;border:1px solid var(--divider-color,#ccc);cursor:pointer;margin-right:4px}
        .pill.on{background:var(--primary-color,#03a9f4);color:#fff;border-color:transparent}
        .sw{display:inline-block;width:12px;height:12px;border-radius:3px;margin:0 4px 0 10px;vertical-align:-1px}`;

  class PlanCard extends Base {
    static getStubConfig() { return { floors: [{ name: 'Stueplan', width: 700, height: 500, rooms: [{ name: 'Stue', rect: [0, 0, 400, 300] }] }] }; }
    _setK(k) { this._k = +k || 0; this._draw(); }
    body(h, vm) {
      const c = this._c; const k = this._k || 0; const hrs = c.hours || [0, 3, 6, 12];
      let o = `<div class="sm" style="margin:2px 0 8px">${hrs.map((x) => `<span class="pill${x === k ? ' on' : ''}" data-k="${x}">${x === 0 ? 'nu' : `om ${x} t`}</span>`).join(' ')}</div>`;
      const tval = (r) => { const d = room(h, vm, r); return { d, t: k === 0 ? d.t : (d.fc && d.fc.length >= k ? d.fc[k - 1] : null) }; };
      if (!(c.floors || []).length) {
        // Ingen plantegning: rummene som felter pr. varmekilde – virker i ethvert hus uden opsætning
        for (const src of vm.sources.filter((x) => !c.source || x.name === c.source)) {
          o += `<div class="h2">${esc(src.name)}</div><div class="tg">` + src.rooms.map((r) => {
            const { d, t } = tval(r); const a = d.a;
            const lim = [num(a.gulv) !== null ? `gulv ${fmt(a.gulv)}` : '', num(a.loft) !== null ? `loft ${fmt(a.loft)}` : ''].filter(Boolean).join(' · ');
            return `<div class="tile" style="background:${TCOL(t, num(a.gulv), num(a.loft))}" data-e="${esc(d.tE || r.model)}"><div>${esc(r.name)}</div>`
              + `<div class="tv">${t === null ? '–' : `${k === 0 && d.est ? '≈' : ''}${fmt(t)} °C`}</div><div class="tsm">${lim}</div></div>`;
          }).join('') + '</div>';
        }
      }
      o += '<div class="pl">';
      for (const f of c.floors || []) o += `<div class="pf"><div class="h2">${esc(f.name || '')}</div>${floorSvg(h, vm, f, k)}</div>`;
      o += '</div>';
      o += `<div class="sm mut bleg" style="margin-top:6px"><span class="grad"></span><span>under gulvet</span><span>godt</span><span>loftet</span>${k ? ' · forudsigelse' : ''} · rød prik = føler</div>`;
      // Valgfri varmekilde: planen time for time og den anbefalede styring (anlæg pr. varmepumpe)
      const s = c.source ? vm.sources.find((x) => x.name === c.source) : null;
      if (s && s.plan) {
        const pa = at(h, s.plan); const ca = at(h, s.e('command')); const cst = (st(h, s.e('command')) || {}).state;
        const pl = (pa.plan || []).slice(0, 24); const mx = Math.max(0.1, ...pl.map((x) => x.heat_kw || 0));
        o += `<div class="h2">${esc(s.name)}: plan</div><div class="tl">${pl.map((x) => `<div class="tc${x.blocked ? ' rd' : ''}" title="${esc(`${when(x.t)}: ${fmt(x.heat_kw, 2)} ${s.kind === 'hydronic' ? 'kW varme' : 'kWh el'} · ${fmt(x.price, 2)} kr${x.blocked ? ' · blokeret' : ''}${x.preload ? ` · opvarmning før rød` : ''}`)}"><div class="tb" style="height:${Math.round(100 * (x.heat_kw || 0) / mx)}%${x.preload ? `;background:repeating-linear-gradient(135deg,${PU} 0 4px,${OR} 4px 8px)` : ''}"></div><div class="tt">${hh(new Date(x.t).getHours())}</div></div>`).join('')}</div>`;
        o += `<div class="sm" style="margin-top:4px"><span class="v" data-e="${esc(s.e('command'))}">Anbefalet: ${cst === 'heat' ? 'varme' : cst === 'off' ? 'slukket' : '–'}${ca.setpoint != null ? ` · setpunkt ${fmt(ca.setpoint)} °C` : ''}</span> · ${esc(ca.reason || '')}<br>${preText(h, s) ? `<b>${esc(preText(h, s))}</b><br>` : ''}${esc(pa.reason || '')}</div>`;
      }
      return o;
    }
    get css() {
      return PCSS + `
        .tg{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;margin-bottom:8px}
        .tile{border-radius:10px;padding:8px 10px;cursor:pointer;color:#111}.tv{font-size:20px;font-weight:700}.tsm{font-size:11px;color:#333}
        .tl{display:flex;gap:2px;height:52px;align-items:flex-end}.tc{flex:1;position:relative;height:100%;background:var(--secondary-background-color,#f4f4f4);border-radius:3px;display:flex;flex-direction:column;justify-content:flex-end}
        .tc.rd{box-shadow:inset 0 0 0 2px #d6453d}.tb{background:#5b9bd5;border-radius:3px 3px 0 0}.tt{font-size:9px;text-align:center;color:var(--secondary-text-color,#666)}`;
    }
  }

  // ------------------------------------------------------------------ rum
  class RumCard extends Base {
    static getStubConfig() { return {}; }
    body(h, vm) {
      const c = this._c; const compact = !!c.compact;
      const groups = c.groups
        ? c.groups.map((g) => ({ name: g.name, rooms: (g.rooms || []).map((n) => vm.rooms.find((r) => r.name === n)).filter(Boolean) }))
        : vm.sources.map((s) => ({ name: s.name + (s.kind === 'hydronic' ? ' · vandbåren' : ' · luft-luft'), rooms: s.rooms }));
      let o = '';
      for (const g of groups) {
        const ds = g.rooms.map((r) => room(h, vm, r));
        const checked = ds.filter((d) => d.chk); const hit = checked.filter((d) => d.status === 'ramt').length;
        o += `<div class="h2">${esc(g.name)}${checked.length ? ` <span class="mut sm" style="text-transform:none;letter-spacing:0">· forudsigelsen ramt i ${hit} af ${checked.length}</span>` : ''}</div><div class="wrap"><table>`;
        o += '<tr><th>Rum</th><th class="n">Nu</th><th class="n">Ventet nu</th><th>Holdt?</th><th class="n">Om 3 t</th><th class="n">Om 6 t</th>'
          + (compact ? '' : '<th class="n">Laveste ventet</th><th class="n">Afkøling °C/t ved 20 K</th><th class="n">Tidskonstant</th><th class="n">°C pr. kWh varme (luft-luft: kWh el)</th><th class="n">Varme pr. døgn</th><th class="n">Præcision 6 t</th><th>Telestat</th>') + '</tr>';
        for (const d of ds) {
          const a = d.a; const per = a.varmekilder_grader_pr_kwh || {};
          o += `<tr><td><span class="v" data-e="${esc(d.r.model)}">${esc(d.r.name)}</span>${d.chk && d.chk.flag ? `<span class="flag" data-e="${esc(d.r.model)}" title="afviger mere end dobbelt så meget som normalt">!</span>` : ''}</td>`
            + `<td class="n">${d.est ? `<span class="v amber" data-e="${esc(d.tE)}" title="${esc(estTip(d))}">${tTxt(d)}</span>` : M(h, d.tE, 1, '°C', d.cls)}</td>`
            + `<td class="n">${d.chk ? `<span title="${esc(String(d.chk.grundlag || '').startsWith('målt') ? `regnet med ${d.chk.grundlag === 'målt el' ? 'den målte el' : 'den målte varme'} · planen ventede ${fmt(d.chk.ventet_med_plan)} °C` : 'regnet med planens varme')}">${fmt(d.chk.ventet)}</span>` : '–'}</td><td class="${d.cls}">${statusTxt(d)}</td>`
            + `<td class="n">${fmt(d.f3)}</td><td class="n">${fmt(d.f6)}</td>`;
          if (!compact) {
            o += `<td class="n">${d.low === null ? '–' : `${fmt(d.low)} <span class="mut sm">${when(d.lowT)}</span>`}</td>`
              + `<td class="n">${V(h, d.r.model, 3)}</td>`
              + `<td class="n">${a.tidskonstant_timer != null ? A(h, d.r.model, a.tidskonstant_timer, 0, 't') : '–'}</td>`
              + `<td class="n">${Object.entries(per).map(([k, v]) => `${A(h, d.r.model, v, 3)} <span class="mut sm">${esc(k)}</span>`).join('<br>') || '–'}</td>`
              + `<td class="n">${d.r.heat ? `${V(h, d.r.heat, 1, 'kWh')} <span class="mut sm">${fmt(at(h, d.r.heat).andel_af_gulvvarmen_pct, 0)} %</span>` : '–'}</td>`
              + `<td class="n">${P6(a) != null ? `<span title="${a['præcision_6_timer_afprøvet'] != null ? `afprøvet på ${a['afprøvede_vinduer']} gange 6 timer · skævhed ${sgn(a['skævhed_6_timer_afprøvet'], 2)} °C · fittet ±${fmt(a['præcision_6_timer'], 2)}` : 'fra fittet'}">±${A(h, d.r.model, P6(a), 2, '°C')}</span>` : '–'}</td>`
              + `<td>${d.valve === 'altid åben' ? 'altid åben' : d.valve ? `<span class="v ${d.vs === 'on' ? 'orange' : ''}" data-e="${esc(d.valve)}">${d.vs === 'on' ? 'åben' : d.vs === 'off' ? 'lukket' : '–'}</span>` : '–'}</td>`;
          }
          o += '</tr>';
        }
        o += '</table></div>';
      }
      return o + LEG;
    }
  }

  // ------------------------------------------------------------------ varmekilder
  function copBars(days, e) {
    const ds = [...days].reverse(); if (!ds.length) return '';
    const W = 640, Hh = 150, n = ds.length, bw = (W - 40) / n;
    const mx = Math.max(4, ...ds.map((d) => Math.max(d.cop || 0, d.stabil || 0)));
    const y = (v) => Hh - 22 - ((Hh - 40) * v) / mx;
    let g = '';
    ds.forEach((d, i) => {
      const x = 30 + i * bw;
      if (d.cop != null) g += `<rect x="${x + 3}" y="${y(d.cop)}" width="${bw - 6}" height="${Hh - 22 - y(d.cop)}" rx="2" fill="${GR}"><title>${esc(d.dato)}: COP ${fmt(d.cop, 2)} · forventet ${fmt(d.stabil, 2)} · ude ${fmt(d.ude)} °C</title></rect>`;
      if (d.stabil != null) g += `<line x1="${x + 2}" x2="${x + bw - 2}" y1="${y(d.stabil)}" y2="${y(d.stabil)}" stroke="${OR}" stroke-width="3"/>`;
      g += `<text x="${x + bw / 2}" y="${Hh - 8}" text-anchor="middle">${esc(String(d.dato).slice(8, 10))}</text>`;
    });
    for (let v = 1; v <= mx; v++) g += `<text x="22" y="${y(v) + 4}" text-anchor="end">${v}</text><line x1="26" x2="${W}" y1="${y(v)}" y2="${y(v)}" stroke="var(--divider-color)" stroke-width="1"/>`;
    return `<div class="h2">COP pr. døgn – 14 døgn</div><svg class="bars" viewBox="0 0 ${W} ${Hh}" style="width:100%;cursor:pointer" data-e="${esc(e)}">${g}</svg>`
      + `<div class="leg"><span style="color:${GR}">■</span> COP · <span style="color:${OR}">▬</span> forventet ved stabil drift</div>`;
  }
  class KildeCard extends Base {
    static getStubConfig() { return {}; }
    body(h, vm) {
      const only = this._c.source; let o = '';
      for (const s of vm.sources.filter((x) => !only || x.name === only)) {
        o += `<div class="h">${esc(s.name)} <span class="mut sm">${s.kind === 'hydronic' ? 'vandbåren' : 'luft-luft'}</span></div>`;
        o += s.kind === 'hydronic' ? this.hyd(h, s) : this.air(h, vm, s);
      }
      return o + LEG;
    }
    hyd(h, s) {
      const y = s.e('cop_yesterday'), ya = at(h, y), sd = s.e('cop_steady'), sa = at(h, sd), hl = s.e('heat_loss'), ha = at(h, hl);
      const td = s.e('cop_today'), ta = at(h, td);
      const row = (k, v) => `<div>${k}</div><div class="n">${v}</div>`;
      let o = '<div class="h2">Effektivitet</div><div class="grid">';
      o += row('COP i dag', `${V(h, td, 2)} <span class="mut sm">${fmt(num(ta.kwh_varme), 1)} kWh varme / ${fmt(num(ta.kwh_el), 1)} kWh el</span>`);
      o += row('– uden varmt vand / stabil drift', `${A(h, td, ta.cop_uden_varmt_vand, 2)} / ${A(h, td, ta.cop_stabil_drift, 2)} <span class="mut sm">ekstra el ${fmt(num(ta.ekstra_el_opstart_afrimning_standby_pct), 1)} %</span>`);
      o += row('COP i går', `${V(h, y, 2)} <span class="mut sm">${fmt(ya.varme_kwh, 0)} kWh varme / ${fmt(ya.el_kwh, 1)} kWh el · ude ${fmt(ya.ude_snit)} °C</span>`);
      o += row('– uden varmt vand / model', `${A(h, y, ya.cop_uden_varmt_vand, 2)} / ${A(h, sd, ya.forventet_stabil_cop, 2)} <span class="mut sm">(${sgn(num(ya.forskel_fra_stabil_pct))} %)</span>`);
      o += row('COP 7 døgn', V(h, s.e('cop_week'), 2));
      o += row('Stabil drift, reference A0/W35 4 kW', V(h, sd, 2));
      o += row('Pr. grad lavere fremløb', A(h, sd, sa.per_k_flow_pct, 1, '% bedre COP'));
      o += row('Ekstra el ved opstart, afrimning og standby (10 dage)', V(h, s.e('cop_loss'), 1, '%'));
      o += row('Afrimninger pr. kompressortime', V(h, s.e('defrost'), 2));
      o += '</div>' + copBars(ya['seneste_14_døgn'] || [], y);
      o += '<div class="h2">Bygning</div><div class="grid">';
      o += row('Varmetab', `${V(h, hl, 1, 'W/K')} <span class="mut sm">målt ved ${fmt(ha.ude_fra)} til ${fmt(ha.ude_til)} °C ude</span>`);
      o += row('Tilskud uden for varmepumpen', A(h, hl, ha.gains_w, 0, 'W'));
      o += row('Afkølingstid', V(h, s.e('cooling'), 0, 'timer'));
      o += row('Ekstra varmetab pr. m/s vind', V(h, s.e('wind_effect'), 1, '%'));
      o += '</div><div class="h2">Anlæg</div><div class="grid">';
      o += row('Største effekt', V(h, s.e('max_power'), 2, 'kW'));
      o += row('Laveste stabile effekt', V(h, s.e('min_power'), 2, 'kW'));
      o += row('Gulvets afgivelse', V(h, s.e('emission'), 3, 'kW/K'));
      o += row('Lært fremløb ved 0 °C ude', V(h, s.e('flow_learned'), 1, '°C'));
      return o + '</div>';
    }
    air(h, vm, s) {
      const y = s.e('el_yesterday'), f = sensorsOf(h, s);
      const row = (k, v) => `<div>${k}</div><div class="n">${v}</div>`;
      let o = '<div class="grid">';
      o += row('El i går', `${V(h, y, 1, 'kWh')} <span class="mut sm">ude ${fmt(at(h, y).ude_snit)} °C</span>`);
      o += row('El pr. døgn, 7 døgn', V(h, s.e('el_week'), 1, 'kWh'));
      if (f.unit_temp) o += row('Temperatur ved indedelen', M(h, f.unit_temp, 1, '°C'));
      o += '</div><div class="h2">Rum den varmer</div><div class="wrap"><table><tr><th>Rum</th><th class="n">Nu</th><th class="n">°C pr. kWh el</th><th class="n">Afkøling °C/t ved 20 K</th><th class="n">Præcision 6 t</th></tr>';
      for (const r of s.rooms) {
        const d = room(h, vm, r); const a = d.a;
        o += `<tr><td><span class="v" data-e="${esc(r.model)}">${esc(r.name)}</span></td><td class="n">${M(h, d.tE, 1, '°C', d.cls)}</td>`
          + `<td class="n">${A(h, r.model, (a.varmekilder_grader_pr_kwh || {})[s.name], 3)}</td><td class="n">${V(h, r.model, 3)}</td>`
          + `<td class="n">${P6(a) != null ? '±' + A(h, r.model, P6(a), 2, '°C') : '–'}</td></tr>`;
      }
      return o + '</table></div>';
    }
  }

  // ------------------------------------------------------------------ energitiltag
  class TiltagCard extends Base {
    static getStubConfig() { return {}; }
    body(h, vm) {
      let o = '<div class="h2">Hvor taber rummene varmen hurtigst?</div><div class="wrap"><table><tr><th>Rum</th><th class="n">Falder °C/t ved 20 K</th><th class="n">Tidskonstant</th><th class="n">Brat afkøling pr. døgn</th><th class="n">Udluftninger</th><th>Varmekilde</th></tr>';
      const rows = vm.rooms.map((r) => ({ r, a: at(h, r.model), v: sv(h, r.model) })).filter((x) => x.v !== null).sort((x, y) => y.v - x.v);
      for (const x of rows) {
        const ha = x.r.heat ? at(h, x.r.heat) : {};
        o += `<tr><td><span class="v" data-e="${esc(x.r.model)}">${esc(x.r.name)}</span></td><td class="n">${V(h, x.r.model, 3)}</td>`
          + `<td class="n">${x.a.tidskonstant_timer != null ? A(h, x.r.model, x.a.tidskonstant_timer, 0, 't') : '–'}</td>`
          + `<td class="n">${ha['brat_afkøling_pr_døgn'] != null ? A(h, x.r.heat, ha['brat_afkøling_pr_døgn'], 1) : '–'}</td>`
          + `<td class="n">${x.a.udeladt_udluftning != null ? A(h, x.r.model, x.a.udeladt_udluftning, 0) : '–'}</td><td>${esc(x.r.src.name)}</td></tr>`;
      }
      o += '</table></div><div class="leg">Tidskonstant: timer før rummet uden varme er faldet ca. 2/3 af vejen mod udetemperaturen – lav betyder, at rummet taber varmen hurtigt (utæt, kolde flader eller lille varmelager). '
        + 'Brat afkøling: gange pr. døgn temperaturen falder pludseligt (døre, vinduer, vaner). Udluftninger: 6-timers perioder med udluftning, som modellen har sprunget over.</div>';
      // hvad er en grad og et tiltag værd
      for (const s of vm.sources.filter((x) => x.kind === 'hydronic')) {
        const H = sv(h, s.e('heat_loss')); const cop = sv(h, s.e('cop_week')) || 2.5; const pa = at(h, s.plan);
        const pris = pa.el_kwh ? pa.cost_kr / pa.el_kwh : null; const ude = num(at(h, s.e('cop_yesterday')).ude_snit) ?? 3;
        if (H === null) continue;
        const k1 = (H * 24) / 1000, k10 = (0.1 * H * (20.5 - ude) * 24) / 1000;
        o += `<div class="h2">${esc(s.name)}: hvad er en grad eller et tiltag værd?</div><div class="wrap"><table><tr><th>Hvis …</th><th class="n">Varme pr. døgn</th><th class="n">El pr. døgn</th><th class="n">Pr. måned</th></tr>`
          + `<tr><td>1 °C lavere inde</td><td class="n">${fmt(k1)} kWh varme</td><td class="n">${fmt(k1 / cop)} kWh el</td><td class="n">${pris ? 'ca. ' + fmt((k1 / cop) * pris * 30, 0) + ' kr' : '–'}</td></tr>`
          + `<tr><td>10 % mindre varmetab</td><td class="n">${fmt(k10)} kWh varme</td><td class="n">${fmt(k10 / cop)} kWh el</td><td class="n">${pris ? 'ca. ' + fmt((k10 / cop) * pris * 30, 0) + ' kr' : '–'}</td></tr></table></div>`
          + `<div class="leg">Varmetab ${V(h, s.e('heat_loss'), 0, 'W/K')} · COP ${fmt(cop, 2)} (7 døgn) · elpris ${fmt(pris, 2)} kr/kWh el (planens snit) · ${fmt(ude)} °C ude (i går).</div>`;
      }
      // vind
      for (const s of vm.sources.filter((x) => x.kind === 'hydronic')) {
        const we = s.e('wind_effect'); const sec = at(h, we).sectors;
        o += `<div class="h2">Vind</div><div class="grid"><div>Ekstra varmetab pr. m/s vind</div><div class="n">${V(h, we, 1, '%')}</div>`;
        const items = sec && typeof sec === 'object' ? Object.entries(sec).filter(([, v]) => v !== null && typeof v !== 'object') : [];
        if (items.length) for (const [k, v] of items) o += `<div>Fra ${esc(k)}</div><div class="n">${A(h, we, v, 3)}</div>`;
        o += '</div>' + (items.length ? '' : '<div class="leg">Vindretningerne kræver flere dage med både vind og pauser i varmen.</div>');
      }
      // før/efter
      const ms = vm.e('measures'); const ma = at(h, ms); const list = ma.tiltag || [];
      o += '<div class="h2">Før og efter tiltag</div>';
      if (!ma['kalender']) o += '<div class="mut">Vælg en kalender med tiltag under Varmemodels <i>Konfigurer</i>.</div>';
      else {
        o += `<div class="wrap"><table><tr><th>Tiltag</th><th>Etage</th><th>Dato</th><th class="n">Før</th><th class="n">Efter</th><th class="n">Ændring</th><th>Data</th></tr>`;
        for (const m of list) {
          const u = m.enhed || 'W/K';
          o += `<tr><td>${esc(m.navn)}</td><td>${esc(m.kilde || '')}</td><td>${esc(m.dato)}</td><td class="n">${A(h, ms, m['før_w_k'], 1, u)}</td><td class="n">${A(h, ms, m.efter_w_k, 1, u)}</td>`
            + `<td class="n ${m['ændring_pct'] < 0 ? 'green' : m['ændring_pct'] > 0 ? 'red' : ''}">${m['ændring_pct'] == null ? '–' : sgn(m['ændring_pct']) + ' %'}</td>`
            + `<td class="mut sm">${m.dage_før} døgn før · ${m.dage_efter} efter${m.note ? ' · ' + esc(m.note) : ''}</td></tr>`;
        }
        if (!list.length) o += '<tr><td colspan="7" class="mut">Ingen tiltag i kalenderen endnu.</td></tr>';
        o += `</table></div><div class="leg">Tilføj et tiltag som en begivenhed i kalenderen <button class="lnk" data-nav="/calendar">Åbn kalenderen</button>. Skriv varmekildens navn, et rums navn eller »1. sal« i titel eller beskrivelse, så måles tiltaget på den etage – ellers på den vandbårne. Varmetabet (vandbåren: W/K varme; luft-luft: W el/K) regnes for sig før og efter datoen (kræver ca. 16 døgn på hver side) ved næste nats kalibrering.</div>`;
      }
      return o + LEG;
    }
  }

  // ------------------------------------------------------------------ hus
  const STATUS = { waiting: 'venter', ok: 'ok', partial: 'delvis', insufficient: 'for lidt data', frozen: 'frosset', error: 'fejl' };
  const PS = { heat: 'varmer', off: 'slukket', blocked: 'blokeret', unknown: 'ingen plan' };
  class HusCard extends Base {
    static getStubConfig() { return {}; }
    body(h, vm) {
      const sa = at(h, vm.status); const sst = (st(h, vm.status) || {}).state;
      const row = (k, v) => `<div>${k}</div><div class="n">${v}</div>`;
      let o = '<div class="h2">Varmemodel</div><div class="grid">';
      o += row('Kalibrering', `<span class="v ${sst === 'ok' ? 'green' : 'amber'}" data-e="${esc(vm.status)}">${esc(STATUS[sst] || sst)}</span> · ${esc((st(h, vm.e('mode')) || {}).state === 'frozen' ? 'tallene er frosset' : 'lærer løbende')}`);
      o += row('Seneste kalibrering', sa.seneste_kalibrering ? when(sa.seneste_kalibrering) : '–');
      o += row('Data', `${sa.opsamlede_timer ?? '–'} timer siden ${sa.opsamlet_fra ? new Date(sa.opsamlet_fra).toLocaleDateString('da-DK') : '–'}`);
      o += row('Holdt tilbage af vagten', esc((sa['begrænsede_tal'] || []).join(', ') || 'intet'));
      o += row('Ude nu', M(h, (sa['følere'] || {}).ude, 1, '°C'));
      o += '</div>';
      for (const s of vm.sources.filter((x) => x.kind === 'hydronic')) {
        const pa = at(h, s.plan); const ps = (st(h, s.plan) || {}).state;
        const up = pa.updated ? Math.round((Date.now() - Date.parse(pa.updated)) / 60000) : null;
        o += `<div class="h2">Plan · ${esc(s.name)}</div><div class="grid">`;
        o += row('Nu', `<span class="v" data-e="${esc(s.plan)}">${esc(PS[ps] || ps)}</span>${pa.heat_kw ? ` · ${fmt(pa.heat_kw)} kW` : ''}`);
        o += row('Begrundelse', `<span class="sm">${esc(pa.reason || '–')}</span>`);
        o += row('Opdateret', up === null ? '–' : `${up} min. siden · input ${pa.inputs_ok ? 'ok' : 'mangler ' + esc((pa.missing || []).join(', '))}`);
        o += row('Periode', `${pa.horizon_h ?? '–'} timer · ca. ${fmt(pa.cost_kr, 0)} kr`);
        o += row('Jeg fryser', pa.boost ? `+${fmt(pa.boost_delta)} °C til ${when(pa.boost_until)}` : 'fra');
        const ce = s.e('command'); const ca = at(h, ce);
        if (ce) o += row('Anbefalet styring', `<span class="v" data-e="${esc(ce)}">${(st(h, ce) || {}).state === 'heat' ? 'varme' : 'slukket'}${ca.soft_start_flow != null && ca.flow_setpoint != null ? ` · blød start ${fmt(ca.soft_start_flow, 0)} → ${fmt(ca.flow_setpoint, 0)} °C (+2 °C pr. kvarter)` : ca.flow_setpoint != null ? ` · fremløb ${fmt(ca.flow_setpoint, 0)} °C` : ''}</span> <span class="mut sm">${esc(ca.reason || '')}</span>`);
        if (ce) o += row('I dag', `${ca.writes_today ?? 0} skrivninger · ${ca.switches_today ?? 0} tænd/sluk ${ca.within_limits === false ? '<span class="amber">over grænsen</span>' : '<span class="green">inden for grænsen</span>'}`);
        o += row('Frisk plan i dag', `${fmt(pa.fresh_pct_today, 0)} % af kvartererne`);
        o += '</div>';
      }
      o += '<div class="h2">Planens indstillinger</div><div class="grid">';
      for (const [k, n, u] of [['max_pause', 'Maks. pause uden gulvvarme', 't'], ['comfort_offset', 'Rum må svinge over gulvet med', '°C'], ['boost_delta', 'Jeg fryser hæver med', '°C'],
        ['boost_hours', 'Jeg fryser gælder højst', 't'], ['red_allowance', 'Må gå under gulvet i røde timer med', '°C'], ['economy', 'Økonomi: må gå under gulvet med', '°C'], ['cold_caution', 'Forsigtighed ved kulde', '%'], ['flow_margin', 'Maks. fremløb over det lærte', '°C']]) {
        const e = vm.e(k); o += row(n, e ? `<span class="v" data-e="${esc(e)}">${fmt(sv(h, e))} ${u}</span>` : '–');
      }
      return o + '</div>';
    }
  }

  // ------------------------------------------------------------------ anlæg
  const ACSS = `
  .band{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:6px;font-size:14px;line-height:1.35}
  .b{background:var(--secondary-background-color);border:1px solid var(--divider-color);border-radius:10px;padding:7px 12px}
  .zb{color:#fff;font-weight:700}.zb .z{font-size:16px}.zb .sub{font-weight:500;font-size:13px;opacity:.95}
  .afv{flex:1 1 240px}.zn{flex:1 1 380px}.note{flex:1 1 220px}
  .zl{display:grid;grid-template-columns:86px 1fr;align-items:center;column-gap:8px;margin-top:3px}
  .zr{display:flex;gap:1px;height:13px}.zr.v{height:8px}.zr i{flex:1;border-radius:2px}
  .zr i.p{filter:saturate(.35) brightness(.85)}.zr i.n{outline:2px solid var(--primary-text-color)}
  .zt{position:relative;height:13px;font-size:11px;color:var(--secondary-text-color)}.zt span{position:absolute;transform:translateX(-50%)}
  details summary{cursor:pointer;color:var(--secondary-text-color);font-size:12px;margin-top:4px}
  .acts{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
  .acts button{font:inherit;font-size:13px;font-weight:600;color:var(--primary-text-color);background:var(--card-background-color);border:2px solid var(--primary-color);border-radius:8px;padding:5px 10px;cursor:pointer}
  .acts button.on{background:${OR};border-color:${OR};color:#fff}
  svg.an{width:100%;height:auto;display:block}
  svg.an text{fill:var(--primary-text-color);font-size:15px}
  svg.an .m{fill:var(--secondary-text-color);font-size:13px}
  svg.an .c{text-anchor:middle}svg.an .r{text-anchor:end}
  svg.an .ttl{font-size:18px;font-weight:600;text-anchor:middle}
  svg.an .hdr{font-size:17px;font-weight:600}
  svg.an .box{fill:none;stroke:var(--secondary-text-color);stroke-width:2}
  svg.an .comp{fill:var(--card-background-color);stroke:var(--secondary-text-color);stroke-width:2}
  svg.an .room{fill:var(--secondary-background-color);stroke:var(--divider-color);stroke-width:1.5}
  svg.an .hot{stroke:${OR};stroke-width:5;fill:none}
  svg.an .cold{stroke:#3d9be0;stroke-width:5;fill:none}
  svg.an .idle{stroke:var(--divider-color);stroke-width:3;fill:none}
  svg.an .ovl{stroke:${OR};stroke-width:3;fill:none;stroke-dasharray:9 6}
  svg.an .air{stroke:#8fc9ef;stroke-width:3;fill:none;stroke-dasharray:4 5}
  svg.an .pill rect{fill:var(--card-background-color);stroke:var(--divider-color);stroke-width:1.5}
  svg.an .pill text{font-size:17px;font-weight:600;text-anchor:middle}
  svg.an .pill.usikker{opacity:.45}
  svg.an [data-e]{cursor:pointer}
  svg.an .sen{fill:#14a89a}svg.an .tag{fill:#fff;font-size:14px;font-weight:700;text-anchor:middle}
  svg.an .rn{font-size:17px;font-weight:600}svg.an .rt{font-size:21px;font-weight:700}
  svg.an .vt{fill:var(--secondary-text-color);font-size:13px}
  svg.an .gulv{fill:var(--primary-color);cursor:pointer;text-decoration:underline dotted}
  svg.an .act{cursor:pointer}svg.an .act rect{fill:var(--card-background-color);stroke:var(--primary-color);stroke-width:2}
  svg.an .act text{font-size:14px;font-weight:600}svg.an .act.on rect{fill:${OR};stroke:${OR}}svg.an .act.on text{fill:#fff}
  svg.an .flg{fill:${RE};font-weight:800;font-size:19px}
  `;
  const TX = (x, y, s, cls = '', ex = '') => `<text x="${x}" y="${y}" class="${cls}"${ex}>${esc(s)}</text>`;
  const BOX = (x, y, w, ht, t, sub) => `<rect x="${x}" y="${y}" width="${w}" height="${ht}" rx="8" class="box"/>` + (t ? TX(x + w / 2, y + 26, t, 'ttl') : '') + (sub ? TX(x + w / 2, y + 44, sub, 'm c') : '');
  const PATH = (d, cls, a1, a0) => `<path d="${d}" class="${cls}"${a1 ? ' marker-end="url(#vmah)"' : ''}${a0 ? ' marker-start="url(#vmah)"' : ''}/>`;
  const PILL = (x, y, w, ht, s, e, o = {}) => `<g class="pill${o.cls ? ' ' + o.cls : ''}"${e ? ` data-e="${esc(e)}"` : ''}><rect x="${x - w / 2}" y="${y - ht / 2}" width="${w}" height="${ht}" rx="8"${o.bg ? ` style="fill:${o.bg};stroke:${o.bg}"` : ''}/><text x="${x}" y="${y + 6}"${o.bg ? ' style="fill:#fff"' : ''}>${esc(s)}</text></g>`;
  const SEN = (x, y, l) => `<circle cx="${x}" cy="${y}" r="14" class="sen"/>` + TX(x, y + 5, l, 'tag');
  const TEE = (x, y) => `<circle cx="${x}" cy="${y}" r="13" class="comp"/>`;

  class AnlaegCard extends Base {
    static getStubConfig() { return { buffer: false, bypass: false }; }
    constructor() {
      super();
      this.css = ACSS + PCSS + `
  .pv{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start}.pvl{flex:1 1 300px;max-width:460px;display:flex;flex-direction:column;gap:8px;min-width:0}
  .pvr{flex:2 1 420px;min-width:0}@container (max-width:700px){.pvr{order:-1}}
  .pvh{font-weight:600;margin-bottom:4px}.pvs{font-size:15px;display:flex;align-items:center;gap:6px}.dot{width:12px;height:12px;border-radius:50%;display:inline-block}
  svg.kw{width:100%;height:auto;display:block}svg.kw text{font-size:10px;fill:var(--secondary-text-color);text-anchor:middle}
  .dbs{display:flex;gap:6px;align-items:flex-end;margin-top:8px}.db{flex:1;display:flex;flex-direction:column;align-items:center;font-size:11px}
  .db i{display:block;width:70%;background:#8fc9ef;border-radius:3px 3px 0 0;min-height:1px}
  .trs{display:flex;flex-direction:column;gap:2px;cursor:pointer}.tr{display:grid;grid-template-columns:22px minmax(90px,1fr) 1fr auto;gap:6px;align-items:center;font-size:12px;padding:1px 4px;border-radius:5px}
  .tr.on{background:var(--primary-color);color:#fff}.tr.mt{outline:2px dashed var(--primary-color);outline-offset:-2px}.tr.on .mut{color:#fff}.tn{font-weight:700;text-align:right}
  .tbar{height:8px;background:var(--divider-color);border-radius:4px;overflow:hidden}.tbar i{display:block;height:100%;background:${OR}}.tr.on .tbar i{background:#fff}
  .tw{white-space:nowrap;text-align:right}
  .sg{margin-top:6px}.sgh{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--secondary-text-color);margin:6px 0 2px}
  .sr{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:3px 0;font-size:13px;border-bottom:1px solid var(--divider-color)}
  .sr select,.sr input{font:inherit;font-size:13px;color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:6px;padding:3px 6px}
  .sr input[type=number]{width:70px}
  .tg2{font:inherit;font-size:13px;font-weight:600;border-radius:12px;padding:3px 14px;border:1px solid var(--divider-color);background:var(--card-background-color);color:var(--primary-text-color);cursor:pointer}
  .tg2.on{background:${OR};border-color:${OR};color:#fff}
  .ax{font-size:11px;fill:var(--secondary-text-color,#999)}
  .fsr{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.fsc{border:1px solid var(--divider-color);border-radius:8px;padding:4px 8px}.fsv{font-size:18px;font-weight:600}
  .pv2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:8px;margin-top:10px;align-items:start}
  .bento{display:grid;gap:8px}
  .bt{position:relative;border-radius:12px;padding:10px 12px;background:color-mix(in srgb,var(--c) 16%,var(--card-background-color));border:1px solid color-mix(in srgb,var(--c) 45%,transparent);border-left:5px solid var(--c);min-height:118px;display:flex;flex-direction:column;gap:4px;min-width:0}
  .bh{display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap}.bn{font-weight:600;cursor:pointer}
  .bv{font-size:30px;font-weight:700;line-height:1.1;cursor:pointer;color:var(--primary-text-color)}
  .sp{width:100%;height:44px;display:block}.spm{fill:none;stroke-width:2.2}.spc{fill:none;stroke-width:2.2;stroke-dasharray:4 3;opacity:.85}
  .spf{stroke:var(--secondary-text-color);stroke-width:1;stroke-dasharray:2 3;opacity:.7}.spn{stroke:var(--divider-color);stroke-width:1}
  .bf{display:flex;justify-content:space-between;gap:6px;font-size:12px;color:var(--secondary-text-color);margin-top:auto;flex-wrap:wrap}
  .gl2{color:var(--primary-color);cursor:pointer;text-decoration:underline dotted}
  .uchip{font-size:12px;font-weight:600;border-radius:12px;padding:2px 9px;background:#9aa0a6;color:#fff;cursor:pointer;white-space:nowrap}
  .uchip.heat{background:${OR}}.uchip.defrost{background:${BL}}.fan{display:inline-block;margin-right:4px}
  .fan.spin{animation:vmspin 1.6s linear infinite}@keyframes vmspin{to{transform:rotate(360deg)}}
  .bleg{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.grad{width:120px;height:10px;border-radius:5px;background:linear-gradient(90deg,hsl(212 62% 52%),hsl(150 62% 52%),hsl(140 62% 52%),hsl(42 62% 52%),hsl(4 62% 52%))}
  @container (max-width:640px){.bento{grid-template-columns:1fr 1fr !important}.bt{grid-column:auto !important;grid-row:auto !important}}
  :host{container-type:inline-size}`;
      this._ro = new ResizeObserver(() => { const w = this.clientWidth; if (w && (w < 700) !== this._tall) { this._tall = w < 700; this._draw(); } });
    }
    connectedCallback() { this._ro.observe(this); }
    disconnectedCallback() { this._ro.disconnect(); }
    sc(name) { return (this._c.sources || {})[name] || {}; }
    body(h, vm) {
      // source: én varmekilde pr. visning (fx én pr. etage) – kun den kilde, dens rum, plan og afvigelser
      if (this._c.source) {
        const sources = vm.sources.filter((s) => s.name === this._c.source);
        if (!sources.length) return `<div class="mut">Varmekilden »${esc(this._c.source)}« findes ikke</div>`;
        this._all = vm.sources;
        vm = { ...vm, sources, rooms: sources.flatMap((s) => s.rooms), plan: sources[0].plan };
      }
      const main = vm.sources.find((s) => s.kind === 'hydronic') || null;
      const air = vm.sources.filter((s) => s.kind !== 'hydronic');
      const D = this.data(h, vm, main);
      if (!main && air.length === 1) return this.band(h, vm, main, D) + this.airView(h, vm, air[0]);
      this._fs = main ? at(h, main.e('command')).flow_search || null : null;
      return this.band(h, vm, main, D) + this.svg(h, vm, main, air, D) + this.flowPanel(h, main);
    }
    data(h, vm, main) {
      const D = { outdoor: (at(h, vm.status)['følere'] || {}).ude || null, f: {}, cfg: {}, pa: {}, pst: 'unknown' };
      if (!main) return D;
      const f = sensorsOf(h, main), cfg = this.sc(main.name), pa = at(h, main.plan);
      const pst = (st(h, main.plan) || {}).state || 'unknown';
      const flow = sv(h, f.flow_temp), ret = sv(h, f.return_temp);
      const comp = st(h, f.compressor); const compOn = !!comp && comp.state === 'on';
      // Stabil drift tælles fra kompressorens rigtige start (Varmemodel læser den i historikken), ikke fra last_changed,
      // som en genstart af HA nulstiller – medmindre kompressoren er skiftet efter Varmemodels seneste tjek
      const cmdA = at(h, main.e('command'));
      const lc = comp ? Date.parse(comp.last_changed) : NaN, cs = cmdA.compressor_on_since ? Date.parse(cmdA.compressor_on_since) : NaN;
      const cc = cmdA.compressor_checked ? Date.parse(cmdA.compressor_checked) : NaN;
      const runStart = !isNaN(cs) && !isNaN(cc) && lc <= cc ? Math.min(cs, lc) : lc;
      const onMin = compOn ? (Date.now() - runStart) / 60e3 : 0;
      const defr = isOn(h, f.defrost), dhw = isOn(h, f.dhw);
      const clim = cfg.climate ? st(h, cfg.climate) : null;
      const hpk = kW(h, cfg.heat_power), ep = kW(h, cfg.el_power);
      const steady = compOn && onMin >= 10 && !defr && !dhw && hpk !== null && ep !== null && ep > 0.15;
      const live = steady ? hpk / ep : null;
      const sa = at(h, main.e('cop_steady')); const tout = sv(h, D.outdoor);
      const exp = steady && num(sa.a) !== null && tout !== null && flow !== null ? sa.a + sa.b * tout + sa.c * flow + sa.d * hpk : null;
      this._low = this._low || {};
      if (live !== null && exp !== null && live < exp - 0.5) this._low[main.name] = this._low[main.name] || Date.now(); else delete this._low[main.name];
      const lowMin = this._low[main.name] ? (Date.now() - this._low[main.name]) / 60e3 : 0;
      const planHeat = pst === 'heat', actHeat = compOn && !dhw;
      const differs = pst !== 'unknown' && (planHeat !== actHeat);
      const pumpOn = cfg.pump_running ? isOn(h, cfg.pump_running) : null;
      const bal = compOn && onMin >= 15 && !defr && !dhw && pumpOn !== false;
      Object.assign(D, { f, cfg, pa, pst, flow, ret, compOn, onMin, defr, dhw, clim, hpk, ep, steady, live, exp, lowMin, differs, pumpOn, bal,
        A_: sv(h, cfg.a), B_: sv(h, cfg.b), C_: sv(h, cfg.c), D_: sv(h, cfg.d) });
      return D;
    }
    band(h, vm, main, D) {
      const c = this._c, pa = D.pa, pst = D.pst;
      const z = c.zone ? (st(h, c.zone) || {}).state || '' : ''; const zl = z.toLowerCase();
      const zc = !z ? null : zl.startsWith('r') ? RE : zl.startsWith('gu') || zl.startsWith('y') ? YE : zl.startsWith('g') ? GR : '#6b7278';
      const ico = zc === RE ? '🔴' : zc === YE ? '🟡' : zc === GR ? '🟢' : '';
      const price = c.price ? sv(h, c.price) : num(pa.price);
      const air0 = !main ? vm.sources[0] : null;
      let plan = main ? `${PS[pst] || pst}` : air0 ? this.airPlan(h, air0).replace(/^plan: /, '') : 'ingen varmekilde';
      if (pst === 'heat') plan += ` ${fmt(pa.heat_kw)} kW` + (pa.flow != null ? ` · fremløb ${fmt(pa.flow, 1)} °C` : '') + (pa.next_off ? ` · stop ${when(pa.next_off)}` : '');
      else if (pa.next_on) plan += ` · start ${when(pa.next_on)}`;
      if (pa.boost) plan += ' · jeg fryser' + (pa.boost_until && pa.boost_until !== 'udløbet' ? ` til ${when(pa.boost_until)}` : '');
      const flags = (c.flags || []).filter((x) => (st(h, x.entity) || {}).state === String(x.state ?? 'on')).map((x) => x.text || x.entity);
      const ce = main && main.e('command'); const cs = st(h, ce);
      if (cs && cs.state !== 'unknown' && (cs.state === 'heat') !== (pst === 'heat')) plan += ` · styring: ${cs.state === 'heat' ? 'varme' : 'slukket'} (${cs.attributes.reason || ''})`;
      const bg = zc || 'var(--primary-color)';
      const zone = `<div class="b zb" style="background:${bg};border-color:${bg}"><div class="z">${z ? `${ico} ${esc(z.toUpperCase())} · ` : ''}${fmt(price, 2)} kr/kWh el</div>`
        + `<div class="sub">Plan: ${esc(plan)}${this.dry(h, vm) ? ' (styrer ikke endnu)' : air0 && st(h, air0.e('air_control')) ? ' · Varmemodel styrer' : main && (st(h, main.e('hydro_control')) || {}).state === 'on' ? ' · Varmemodel styrer varmepumpen' : main && st(h, main.e('flow_control')) ? ' · Varmemodel styrer fremløbet' : ''}${flags.length ? ' · ' + esc(flags.join(' · ')) : ''}</div></div>`;
      const notes = (c.notes || []).map((n) => { const v = n.attribute ? at(h, n.entity)[n.attribute] : (st(h, n.entity) || {}).state; return v ? `<div>${esc(v)}</div>` : ''; }).join('');
      // Bred skærm med vandbåren kilde: knapperne står i diagrammets øverste række (se wideSvg)
      const svgActs = !!main && !(this._tall ?? ((this.clientWidth || 1000) < 700));
      const acts = svgActs ? '' : (c.actions || []).map((a) => {
        const s = st(h, a.entity); const on = !!s && s.state === 'on';
        return `<button class="${on ? 'on' : ''}" data-act="${esc(a.entity)}">${esc(a.name || a.entity)}${on ? ' · siden ' + new Date(s.last_changed).toLocaleTimeString('da-DK', { hour: '2-digit', minute: '2-digit' }) : ''}</button>`;
      }).join('');
      return `<div class="band">${zone}<div class="b zn">${this.timeline(h, vm)}</div><div class="b afv">${this.alerts(h, vm, D)}</div>`
        + `${notes ? `<div class="b note">${notes}</div>` : ''}${acts ? `<div class="acts">${acts}</div>` : ''}</div>`;
    }
    timeline(h, vm) {
      const hyd = vm.sources.filter((s) => s.kind === 'hydronic'); const air = vm.sources.filter((s) => s.kind !== 'hydronic');
      if (!hyd.length && !air.length) return '<span class="mut">Ingen plan (ingen varmekilde)</span>';
      const pa0 = at(h, (hyd[0] || air[0]).plan);
      const P = {};
      for (const p of pa0.history || []) P[Date.parse(p.t)] = { price: p.price, blocked: p.blocked };
      for (const p of pa0.plan || []) P[Date.parse(p.t)] = p;
      const keys = Object.keys(P).map(Number);
      if (!keys.length) return '<span class="mut">Ingen plan endnu</span>';
      const pr = keys.map((k) => P[k].price).filter((x) => x != null).sort((a, b) => a - b);
      const q1 = pr[Math.floor(pr.length / 3)], q2 = pr[Math.floor((2 * pr.length) / 3)];
      // Vandbårne kilder i kW varme; luft-luft-kilder i kW el (deres varme måles ikke)
      const HM = [...hyd, ...air].map((s) => {
        const a = at(h, s.plan); const m = {}; const isAir = s.kind !== 'hydronic';
        for (const p of a.history || []) m[Date.parse(p.t)] = p.plan;
        const pre = {};
        for (const p of a.plan || []) { m[Date.parse(p.t)] = isAir ? (p.el_kw ?? p.heat_kw) : p.heat_kw; if (p.preload) pre[Date.parse(p.t)] = p.preload; }
        return { s, m, pre, unit: isAir ? 'kW el' : 'kW varme', none: !(a.plan || []).length, mx: Math.max(isAir ? 0.1 : 1, ...Object.values(m).filter((x) => x != null)) };
      });
      const d0 = new Date(); d0.setHours(0, 0, 0, 0); const nowH = new Date(); nowH.setMinutes(0, 0, 0); const nk = nowH.getTime();
      const cells = (day, fn) => { let o = ''; for (let i = 0; i < 24; i++) { const t = new Date(day); t.setHours(i); const k = t.getTime(); o += fn(k, i, (k < nk ? 'p' : '') + (k === nk ? ' n' : '')); } return o; };
      let rows = '';
      for (let dd = 0; dd < 3; dd++) {
        const day = new Date(d0); day.setDate(day.getDate() + dd); const s0 = day.getTime(), s1 = s0 + 864e5;
        if (!keys.some((k) => k >= s0 && k < s1)) continue;
        const lbl = dd === 0 ? 'I dag' : dd === 1 ? 'I morgen' : day.toLocaleDateString('da-DK', { weekday: 'long' });
        rows += `<div class="zl"><span>${lbl} · pris</span><div class="zr">${cells(day, (k, i, cl) => {
          const p = P[k]; const col = !p ? 'var(--divider-color)' : p.blocked ? RE : p.price <= q1 ? GR : p.price <= q2 ? YE : OR;
          return `<i class="${cl}" style="background:${col}" title="${hh(i)}:00 · ${p ? fmt(p.price, 2) + ' kr/kWh el' + (p.blocked ? ' · blokeret' : '') : 'ingen pris'}"></i>`;
        })}</div></div>`;
        for (const x of HM) {
          if (x.none) { rows += `<div class="zl"><span class="mut sm">↳ ${esc(x.s.name)}</span><span class="mut sm">ingen plan endnu</span></div>`; continue; }
          rows += `<div class="zl"><span class="mut sm">↳ ${esc(x.s.name)}</span><div class="zr v">${cells(day, (k, i, cl) => {
            const v = x.m[k]; const pv = x.pre[k];
            const bg = pv ? `repeating-linear-gradient(135deg,${PU} 0 4px,${OR} 4px 8px)` : v ? OR : 'var(--divider-color)';
            return `<i class="${cl}" style="background:${bg};${v ? `opacity:${(0.35 + (0.65 * v) / x.mx).toFixed(2)}` : ''}" title="${hh(i)}:00 · ${v ? fmt(v) + ' ' + x.unit : 'ingen varme'}${pv ? ` · opvarmning før rød` : ''}"></i>`;
          })}</div></div>`;
        }
      }
      const pre = HM.map((x) => [x.s, preText(h, x.s)]).filter(([, t]) => t);
      if (pre.length) rows += `<div class="zl"><span></span><span class="sm"><span class="sw" style="background:repeating-linear-gradient(135deg,${PU} 0 4px,${OR} 4px 8px)"></span>${pre.map(([x, t]) => `${esc(x.name)}: ${esc(t)}`).join(' · ')}</span></div>`;
      rows += `<div class="zl"><span></span><div class="zt">${[0, 6, 12, 18, 24].map((x) => `<span style="left:${(x / 24) * 100}%">${hh(x)}</span>`).join('')}</div></div>`;
      const pa = pa0;
      return rows + `<details id="vmzd"><summary>planens begrundelse ▾</summary><div class="sm" style="margin-top:4px">${esc(pa.reason || '')}<br>`
        + (hyd[0] ? `Behov ${fmt(pa.demand_kwh, 0)} kWh varme · plan ${fmt(pa.heat_kwh, 0)} kWh varme · ${fmt(pa.el_kwh, 0)} kWh el` : `Plan ${fmt(pa.el_kwh, 0)} kWh el`) + ` · ca. ${fmt(pa.cost_kr, 0)} kr over ${pa.horizon_h ?? '–'} timer<br>`
        + `Rum må svinge ${fmt(pa.comfort_offset)} °C over gulvet · må gå ${fmt(pa.red_allowance)} °C under gulvet i røde timer · maks. pause ${pa.max_pause ?? '–'} timer<br>`
        + (() => { if (!hyd[0]) return ''; const ca = at(h, hyd[0].e('command')); return ca.reason !== undefined ? `Anbefalet styring: ${esc(ca.reason)} · i dag ${ca.writes_today ?? 0} justeringer af fremløbet og ${ca.switches_today ?? 0} tænd/sluk (højst 8)<br>` : ''; })()
        + `Frisk plan i dag: ${fmt(pa.fresh_pct_today, 0)} % af kvartererne</div></details>`;
    }
    alerts(h, vm, D) {
      const w = []; const now = Date.now();
      const sa = at(h, vm.status); const sst = (st(h, vm.status) || {}).state;
      if (!['ok', 'frozen'].includes(sst)) w.push([sst === 'partial' ? 'ℹ️' : '❗', `Varmemodel: ${STATUS[sst] || sst || 'mangler'}${sa.note ? ' – ' + sa.note : ''}`]);
      const lc = sa.seneste_kalibrering ? Date.parse(sa.seneste_kalibrering) : 0;
      if (!lc || now - lc > 26 * 3600e3) w.push(['❗', 'Varmemodel er ikke kalibreret i 26 timer']);
      for (const s of vm.sources.filter((x) => x.kind === 'hydronic')) {
        const a = at(h, s.plan); const up = a.updated ? Date.parse(a.updated) : 0;
        if (!up || now - up > 30 * 60e3) w.push(['❗', `${s.name}: planen er over 30 min. gammel`]);
        if (a.inputs_ok === false) w.push(['❗', `${s.name}: planen mangler ${(a.missing || []).join(', ')}`]);
        if (a.safety) w.push(['❗', `${s.name}: nødplan uden priser`]);
        if (a.flow_raise) w.push(['ℹ️', `${s.name}: fremløbsgrænsen er hævet ${fmt(a.flow_raise, 0)} °C, fordi modellen forudser, at rummene ellers kommer under gulvet`]);
        const fb = a.flow_boost;
        if (fb && fb.rum && fb.rum.length) w.push(['⚠️', `Fremløb for lavt: ${fb.rum.join(', ')} har kaldt på varme i over 6 timer og er under gulvet og under det forventede – fremløbsgrænsen er hævet ${fmt(fb.grader, 0)} °C`]);
        else if (fb && fb.grader > 0) w.push(['ℹ️', `${s.name}: fremløbsgrænsen er stadig hævet ${fmt(fb.grader, 0)} °C (reserve) og sænkes 1 °C pr. 6 timer`]);
        if ((a.rooms_held || []).length) w.push(['⚠️', `${s.name}: bruger sidst kendte temperatur for ${a.rooms_held.join(', ')} (føleren svarer ikke)`]);
        for (const [r, e] of Object.entries(a.rooms_estimated || {})) w.push(['❗', `Mangler data: ${r} har ikke målt siden ${whenM(e['sidste_måling'])} (${e.minutter} min.) – planen bruger forudsigelsen ≈${fmt(e['forudsagt'])} °C. Tjek føleren.`]);
        if ((a.rooms_skipped || []).length) w.push(['❗', `${s.name}: ${a.rooms_skipped.join(', ')} er ikke med i planen (føler eller rummodel mangler)`]);
        const ca = at(h, s.e('command')); if (ca.within_limits === false) w.push(['⚠️', `${s.name}: styringen har i dag ${ca.switches_today} tænd/sluk (grænse 8)`]);
        // Retningen afgør forklaringen: koldere end ventet = varme der slipper ud; varmere = varme der kommer udefra
        for (const [r, k] of Object.entries(a.rooms_check || {})) {
          if (!k.flag) continue;
          const base = `${r}: ${fmt(k['målt'])} °C, ventet ${fmt(k.ventet)} °C`;
          if (k.status === 'under') w.push(['❗', `${base} – koldere end ventet: åben dør eller vindue?`]);
          else w.push(['⚠️', `${base} – varmere end ventet: ekstra varme fra sol, mennesker eller en varmekilde, der kører uden om planen?`]);
        }
      }
      if (D.lowMin >= 30) w.push(['⚠️', `COP ${fmt(D.live, 2)} mod forventet ${fmt(D.exp, 2)} i ${Math.round(D.lowMin)} min.`]);
      if (D.differs && !this.dry(h, vm)) w.push(['⚠️', 'Varmepumpen følger ikke planen']);
      const ids = [...Object.entries(D.f).filter(([k, v]) => typeof v === 'string' && v.includes('.') && !k.endsWith('_attribute')).map(([, v]) => v),
        D.outdoor, ...vm.rooms.map((r) => at(h, r.model)['temperaturføler'])].filter(Boolean);
      // Datapunkter væk i over 30 min. står for sig (»Mangler data«); resten under »Svarer ikke«
      const md = at(h, vm.e('missing_data')); const out = md['udeføler'];
      if (out) w.push(['❗', `Mangler data: udeføleren har ikke målt ${out['sidste_måling'] ? 'siden ' + whenM(out['sidste_måling']) : 'endnu'} – planen bruger ${out.bruger}. Tjek føleren.`]);
      for (const [r, x] of Object.entries(md.rum || {})) if (x['forudsagt'] === null || x['forudsagt'] === undefined) w.push(['❗', `Mangler data: ${r} har ikke målt ${x['sidste_måling'] ? 'siden ' + whenM(x['sidste_måling']) : 'endnu'} og kan ikke forudsiges – rummet er ude af planen. Tjek føleren.`]);
      const known = new Set([...Object.values(md.rum || {}).map((x) => x['føler']), out && out['føler']].filter(Boolean));
      const bad = [...new Set(ids)].filter((e) => { const s = st(h, e); return !known.has(e) && (!s || ['unavailable', 'unknown'].includes(s.state)); });
      if (bad.length) w.push(['❗', 'Svarer ikke: ' + bad.map((e) => (at(h, e).friendly_name || e)).join(', ')]);
      for (const x of this._c.alerts || []) { const s = st(h, x.entity); if (s && s.state === String(x.state ?? 'on')) w.push([x.icon || '⚠️', x.text || x.entity]); }
      return w.length ? w.map(([i, t]) => `<div>${i} ${esc(t)}</div>`).join('') : '<div>✅ Ingen afvigelser</div>';
    }
    pills(h, D, main) {
      const p = {}; if (!main) return p;
      const f = D.f, cfg = D.cfg, pa = D.pa;
      let s0, bg = null;
      if (D.clim && D.clim.state === 'off') s0 = 'Fra';
      else if (D.defr) { s0 = '❄ afrimer'; bg = BL; }
      else if (D.dhw) s0 = 'varmt vand';
      else s0 = (D.compOn ? 'varmer' : 'pause');
      p.status = [s0, cfg.climate || f.compressor, { bg }];
      const fsq = at(h, main.e('command')).flow_search;
      p.plan = fsq ? (fsq.slukket ? (fsq.næste_start ? `starter kl. ${fsq.næste_start} ved ca. ${fmt(fsq.mål, 0)} °C` : `starter ved ca. ${fmt(fsq.mål, 0)} °C, når planen varmer`)
        : `søger laveste · mål ${fmt(fsq.mål, 0)} °C · loft ${fmt(fsq.loft, 0)} °C`)
        : `plan: ${PS[D.pst] || D.pst}${D.pst === 'heat' && pa.flow != null ? ` · fremløb ${fmt(pa.flow, 1)} °C` : ''}`;
      p.ind = [`ind ${fmt(D.ret)} °C`, f.return_temp];
      // Målet, der faktisk står i varmepumpen (fremløbsmålet), ikke planens – under blød start: »mål 26 → 35«
      const tgE = f.flow_target, tg = tgE ? (tgE.startsWith('climate.') ? at(h, tgE).temperature : sv(h, tgE)) : null;
      const soft = at(h, main.e('command')).soft_start_flow;
      const running = !(D.clim && D.clim.state === 'off');
      const fsx = at(h, main.e('command')).flow_search;
      const mål = fsx && tg != null ? fmt(tg, 0)
        : tg != null && running ? (soft != null && pa.flow != null ? `${fmt(tg, 0)} → ${fmt(pa.flow, 0)}` : fmt(tg, 0))
        : (D.pst === 'heat' && pa.flow != null ? fmt(pa.flow) : null);
      p.ud = [`ud ${fmt(D.flow)} °C${mål != null ? ' · mål ' + mål : ''}`, tgE || f.flow_temp];
      p.cop = [D.steady ? `${fmt(D.hpk)} kW · COP ${fmt(D.live)} / ${fmt(D.exp)}` : `${fmt(D.hpk)} kW · COP –`, cfg.heat_power || f.heat_energy, { bg: D.lowMin >= 30 ? YE : null }];
      const fr = sv(h, cfg.flow_rate);
      p.flow = [`${fr === null ? '–' : fmt(fr)} L/min · ΔT ${D.flow !== null && D.ret !== null ? fmt(D.flow - D.ret) : '–'} K`, cfg.flow_rate || f.flow_temp];
      p.tank = cfg.tank ? [`${fmt(sv(h, cfg.tank))} °C`, cfg.tank] : null;
      p.vv = [D.dhw ? 'laver varmt vand' : 'nej', f.dhw];
      // Én kilde: Varmemodels »COP i dag« (5-minutters portioner siden midnat)
      const ct = main.e('cop_today'), ca = at(h, ct);
      p.vdag = [fmt(num(ca.kwh_varme)), ct]; p.edag = [fmt(num(ca.kwh_el)), ct];
      p.copd = [fmt(sv(h, ct), 2), ct];
      p.igaar = `i dag u. varmt vand ${fmt(num(ca.cop_uden_varmt_vand), 2)} · stabil drift ${fmt(num(ca.cop_stabil_drift), 2)}`;
      const P2 = (v, e) => (v === null ? null : [`${fmt(v)} °C`, e]);
      p.a = P2(D.A_, cfg.a); p.b = P2(D.B_, cfg.b); p.c = P2(D.C_, cfg.c); p.d = P2(D.D_, cfg.d);
      p.dthus = D.B_ !== null && D.C_ !== null ? [`ΔT hus ${fmt(D.B_ - D.C_)} K`, cfg.c, { cls: D.pumpOn === false ? 'usikker' : '' }]
        : [`ΔT ${D.flow !== null && D.ret !== null ? fmt(D.flow - D.ret) : '–'} K`, f.flow_temp];
      const mix = (v, e) => [`${v.label} ${fmt(v.v)} K`, e, { bg: D.bal ? mixCol(v.v) : null, cls: D.bal ? '' : 'usikker' }];
      p.ab = D.A_ !== null && D.B_ !== null ? mix({ label: 'A−B', v: D.A_ - D.B_ }, cfg.b) : null;
      p.dc = D.D_ !== null && D.C_ !== null ? mix({ label: 'D−C', v: D.D_ - D.C_ }, cfg.d) : null;
      p.balTxt = D.bal ? '✓ retvisende' : `⏸ ${D.pumpOn === false ? 'pumpen står' : !D.compOn ? 'kompressor holder pause' : D.defr ? 'afrimer' : D.dhw ? 'laver varmt vand' : 'indsvinger'}`;
      if (cfg.pump || cfg.pump_running) {
        const ps = cfg.pump ? (st(h, cfg.pump) || {}).state || '' : ''; const k = (ps.match(/(\d)/) || [])[1];
        const txt = D.pumpOn === false ? 'står' : (D.pumpOn ? 'kører' : '') + (k ? (D.pumpOn ? ' · ' : '') + 'trin ' + k : ps ? ' ' + ps : '');
        p.pump = [txt.trim() || '–', cfg.pump_running || cfg.pump, { bg: D.pumpOn ? GR : null }];
      }
      return p;
    }
    // Et rum: navn, telestat, målt mod forudsigelsen fra 6 timer før, og de næste timer
    roomRow(h, vm, d, x, y, w, ht, tall) {
      const col = d.cls === 'blue' ? BL : d.cls === 'orange' ? ORT : '';
      let g = `<rect x="${x}" y="${y}" width="${w}" height="${ht}" rx="8" class="room"${d.passive ? ' style="opacity:.75"' : ''}/>`;
      if (d.valve) g += `<rect x="${x}" y="${y}" width="8" height="${ht}" rx="3" fill="${d.vs === 'on' ? OR : d.vs === 'off' ? '#6b7278' : 'transparent'}"/>`;
      const flag = d.chk && d.chk.flag;
      const nameEx = ` data-e="${esc(d.r.model)}"`;
      const sub1 = d.passive ? (() => { const pl = pulls(h, vm, d.r, d.t); return pl && Math.abs(pl.eff) >= 0.005 ? `${pl.eff >= 0 ? 'varmer' : 'køler'} ${pl.name} ${fmt(Math.abs(pl.eff), 2)} °C/t` : 'opvarmes passivt'; })()
        : d.valve ? (d.valve === 'altid åben' ? 'altid åben' : d.vs === 'on' ? 'telestat åben' : d.vs === 'off' ? 'telestat lukket' : '') : '';
      // Søg laveste fremløb: rummets målte tendens (pil) og ◆ ved rummet, der bestemmer fremløbet
      const fr = this._fs && (this._fs.rum || []).find((x) => x.navn === d.r.name);
      const arrow = fr ? ` · ${fr.tendens >= 0.05 ? '↗' : fr.tendens <= -0.05 ? '↘' : '→'} ${fr.tendens >= 0 ? '+' : ''}${fmt(fr.tendens, 2)}/t` : '';
      const bindsFlow = !!(this._fs && this._fs.bestemmer === d.r.name);
      const sub2 = d.chk ? `ventet ${fmt(d.chk.ventet)} · ${statusTxt(d)}` : 'ingen forudsigelse endnu';
      const fc = d.f3 !== null ? [`om 3 t ${fmt(d.f3)}`, `om 6 t ${fmt(d.f6)}`] : [];
      // Gulvet (rummets mindste temperatur) – klik åbner hjælperen, så det kan justeres
      const gE = d.a.min_entitet || d.r.model; const gv = d.a.min_entitet ? sv(h, d.a.min_entitet) : num(d.a.gulv);
      const G = (gx, gy, cls) => (d.a.i_plan === false || gv === null ? '' : TX(gx, gy, `gulv ${fmt(gv)} °C ✎`, `${cls} gulv`, ` data-e="${esc(gE)}"`));
      if (!tall) {
        const two = !!sub1;
        g += TX(x + 22, y + (two ? 20 : 24), d.r.name, 'rn', nameEx);
        if (flag) g += TX(x + 30 + d.r.name.length * 9.5, y + (two ? 20 : 24), '!', 'flg', nameEx);
        if (bindsFlow) g += `<text x="${x + 30 + d.r.name.length * 9.5 + (flag ? 10 : 0)}" y="${y + (two ? 20 : 24)}" fill="${OR}" font-size="14"><title>Bestemmer fremløbet (tættest på gulvet)</title>◆</text>`;
        if (sub1) g += TX(x + 22, y + 37, sub1 + arrow, 'vt');
        g += TX(x + 22, y + (two ? 51 : 41), sub2, 'vt', col ? ` style="fill:${col}"` : '');
        g += TX(x + 215, y + ht / 2 + 8, tTxt(d), 'rt c', ` data-e="${esc(d.tE)}"${d.est ? ` style="fill:${YE}"` : col ? ` style="fill:${col}"` : ''}`);
        g += G(x + 215, y + ht - 5, 'vt c');
        fc.forEach((s, i) => { g += TX(x + w - 12, y + ht / 2 - 4 + i * 17, s, 'vt r', nameEx); });
      } else {
        g += TX(x + 16, y + 26, d.r.name, 'rn', nameEx);
        if (flag) g += TX(x + 24 + d.r.name.length * 9.5, y + 26, '!', 'flg', nameEx);
        if (sub1) g += TX(x + w - 12, y + 26, sub1.replace('telestat ', ''), 'vt r');
        g += TX(x + 16, y + 58, tTxt(d), 'rt', ` data-e="${esc(d.tE)}"${d.est ? ` style="fill:${YE}"` : col ? ` style="fill:${col}"` : ''}`);
        if (fc.length) g += TX(x + w - 12, y + 56, `${fmt(d.f3)} → ${fmt(d.f6)}`, 'vt r', nameEx);
        g += G(x + w - 12, y + 42, 'vt r');
        g += TX(x + 16, y + 80, sub2, 'vt', col ? ` style="fill:${col}"` : '');
      }
      return g;
    }
    legend(x, y, cols, air) {
      const c = this._c;
      const it = [['line', 'hot', 'fremløb'], ['line', 'cold', 'retur']];
      if (c.bypass) it.push(['line', 'ovl', 'kortslutning']);
      if (air) it.push(['line', 'air', 'luft']);
      it.push(['bar', OR, 'telestat åben'], ['bar', '#6b7278', 'telestat lukket']);
      let cx = x, cy = y, n = 0, g = '';
      for (const [k, cl, l] of it) {
        g += k === 'line' ? `<line x1="${cx}" y1="${cy}" x2="${cx + 26}" y2="${cy}" class="${cl}"/>` : `<rect x="${cx + 9}" y="${cy - 10}" width="8" height="20" rx="3" fill="${cl}"/>`;
        g += TX(cx + 32, cy + 5, l, 'm'); cx += 32 + l.length * 8 + 26; n++;
        if (n === cols) { n = 0; cx = x; cy += 30; }
      }
      return g;
    }
    svg(h, vm, main, air, D) {
      const tall = this._tall ?? ((this.clientWidth || 1000) < 700);
      const r = tall ? this.tallSvg(h, vm, main, air, D) : this.wideSvg(h, vm, main, air, D);
      return `<svg class="an" viewBox="0 0 ${r.w} ${r.h}" preserveAspectRatio="xMidYMin meet"><defs><marker id="vmah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="context-stroke"/></marker></defs>${r.g}</svg>`;
    }
    wideSvg(h, vm, main, air, D) {
      const c = this._c, p = this.pills(h, D, main); let g = '';
      const P = (x, y, w, v, ht = 36) => (v ? PILL(x, y, w, ht, v[0], v[1], v[2] || {}) : '');
      const cols = [{ n: 'ude nu', e: D.outdoor }, ...(c.extra || []).slice(0, 2).map((x) => ({ n: x.name, e: x.entity }))];
      g += BOX(20, 20, 260, 116, cols.length > 1 ? 'Ude og andre steder' : 'Ude');
      cols.forEach((x, i) => { const cx = Math.round(20 + (260 * (i + 0.5)) / cols.length); g += TX(cx, 64, x.n, 'm c') + PILL(cx, 92, 230 / cols.length, 32, `${fmt(sv(h, x.e))} °C`, x.e); });
      if (main) {
        const sc = this.sc(main.name);
        g += BOX(20, 140, 260, 420, main.name, sc.model || 'varmepumpe');
        g += P(150, 211, 236, p.status, 30) + TX(150, 241, p.plan, 'm c', D.differs ? ` style="fill:${YE};font-weight:700"` : '');
        // ud ud for fremløbet (y 260), ind ud for returen (y 470)
        g += P(150, 260, 236, p.ud) + P(150, 330, 236, p.cop) + P(150, 400, 236, p.flow) + P(150, 470, 236, p.ind);
        // Øverste række ved siden af »Ude«: varmt vand og energi i dag (jævner skærmen ud på bred skærm)
        if (D.f.dhw || p.tank) {
          g += BOX(300, 20, 260, 116, 'Varmt vand') + TX(370, 64, 'beholder', 'm c') + TX(495, 64, 'laver nu', 'm c');
          g += (p.tank ? P(370, 92, 110, p.tank) : TX(370, 98, '–', 'c')) + P(495, 92, 130, p.vv) + PATH('M430 136 V160 H284', D.dhw ? 'hot' : 'idle', true);
        }
        // Knapperne i samme række (slår til/fra)
        const acts = c.actions || [];
        if (acts.length) {
          // Én eller to rækker under overskriften; teksten forkortes, så den altid er inde i knappen
          const two = acts.length > 3;
          g += BOX(900, 20, 590, 116, two ? '' : 'Knapper');
          const n = Math.min(acts.length, 3), bw = (550 - (n - 1) * 12) / n, bh = two ? 40 : 38;
          acts.slice(0, 6).forEach((a, i) => {
            const s0 = st(h, a.entity); const on = !!s0 && s0.state === 'on';
            const x = 920 + (i % 3) * (bw + 12), y = two ? 30 + Math.floor(i / 3) * 52 : 64;
            const tid = on ? ' · ' + new Date(s0.last_changed).toLocaleTimeString('da-DK', { hour: '2-digit', minute: '2-digit' }) : '';
            const max = Math.floor((bw - 16) / 8.2);
            let lbl = (a.name || a.entity) + tid;
            if (lbl.length > max) lbl = (a.name || a.entity).length > max ? (a.name || a.entity).slice(0, max - 1) + '…' : (a.name || a.entity);
            g += `<g class="act${on ? ' on' : ''}" data-act="${esc(a.entity)}"><title>${esc((a.name || a.entity) + tid)}</title><rect x="${x}" y="${y}" width="${bw}" height="${bh}" rx="8"/>`
              + TX(x + bw / 2, y + bh / 2 + 5, lbl, 'c') + '</g>';
          });
        }
        g += BOX(580, 20, 300, 116, 'Energi i dag') + TX(636, 64, 'kWh varme', 'm c') + TX(730, 64, 'kWh el', 'm c') + TX(824, 64, 'COP', 'm c');
        g += P(636, 88, 88, p.vdag, 28) + P(730, 88, 88, p.edag, 28) + P(824, 88, 80, p.copd, 28) + TX(730, 124, p.igaar, 'm c');
        // kredsløb: fremløb øverst, retur nederst; buffer, kortslutning og pumpe efter opsætningen
        const bp = !!c.bypass, bf = !!c.buffer;
        g += PATH('M280 260 H928', 'hot', true) + PATH('M930 470 H284', 'cold', true);
        if (bp) {
          g += PATH(bf ? 'M480 273 V370 H670 V428' : 'M480 273 V457', 'ovl', true, true) + TEE(480, 260) + TX(480, 236, c.bypass_name || 'kortslutning', 'm c');
          g += TX(492, bf ? 392 : 360, 'fremløb → retur', 'm') + TX(492, bf ? 410 : 378, 'eller baglæns træk', 'm');
        }
        if (p.pump) g += '<rect x="560" y="220" width="160" height="80" rx="40" class="comp"/>' + TX(640, 252, sc.pump_name || 'Pumpe', 'ttl') + P(640, 280, 136, p.pump, 28);
        if (bf) g += '<rect x="580" y="430" width="180" height="80" rx="8" class="comp"/>' + TX(670, 464, sc.buffer_name || 'Buffer', 'ttl') + TX(670, 486, 'i returen', 'm c');
        if (p.a) g += SEN(360, 260, 'A') + TX(360, 188, 'VP ud', 'm c') + P(360, 214, 96, p.a);
        if (p.b) g += SEN(810, 260, 'B') + TX(810, 188, 'manifold ind', 'm c') + P(810, 214, 96, p.b);
        if (p.d) g += SEN(380, 470, 'D') + P(380, 516, 96, p.d) + TX(380, 550, 'VP ind', 'm c');
        if (p.c) g += SEN(840, 470, 'C') + P(840, 516, 96, p.c) + TX(840, 550, 'manifold retur', 'm c');
        g += BOX(930, 140, 170, 420, sc.manifold_name || 'Manifold', 'gulvvarme');
        g += P(1015, 222, 150, p.dthus) + P(1015, 290, 150, p.ab) + P(1015, 358, 150, p.dc);
        if (p.ab || p.dc) g += TX(1015, 406, p.balTxt, 'm c', D.bal ? ` style="fill:${GR};font-weight:700"` : '');
      }
      // rummene, opdelt efter varmekilde
      // Rumlisten starter under knap-boksen i øverste række (den går til y 136)
      let y = main && (c.actions || []).length ? 166 : 136;
      const block = (s, rowH) => {
        const ds = s.rooms.map((r) => room(h, vm, r)); const chk = ds.filter((d) => d.chk); const hit = chk.filter((d) => d.status === 'ramt').length;
        g += TX(1150, y, `${s.name}${chk.length ? ` · forudsigelse ramt ${hit}/${chk.length}` : ''}`, 'hdr'); y += 14;
        const rows = [];
        // Alle rum samme højde (også passive rum), så de tre linjer altid er inde i rammen
        for (const d of ds) { const ht = rowH; g += this.roomRow(h, vm, d, 1150, y, 340, ht, false); rows.push({ d, y: y + ht / 2 }); y += ht + 8; }
        y += 26; return rows;
      };
      if (!main && air.length) {
        // Kun luft-luft (fx én etage): varmepumpen til venstre, rummene ved siden af
        const rx = 330;
        const blockAt = (s) => {
          const ds = s.rooms.map((r) => room(h, vm, r)); const chk = ds.filter((d) => d.chk); const hit = chk.filter((d) => d.status === 'ramt').length;
          g += TX(rx, y, `${s.name}${chk.length ? ` · forudsigelse ramt ${hit}/${chk.length}` : ''}`, 'hdr'); y += 14;
          const rows = [];
          for (const d of ds) { const ht = d.passive ? 44 : 58; g += this.roomRow(h, vm, d, rx, y, 400, ht, false); rows.push(y + ht / 2); y += ht + 8; }
          y += 26; return rows;
        };
        for (const s of air) {
          const by = Math.max(y, 140); y = by + 10; const sc = this.sc(s.name);
          g += BOX(20, by, 260, 200, s.name, sc.model || 'luft-luft');
          const cl = sc.climate ? st(h, sc.climate) : null;
          const ctx = !cl ? '–' : cl.state === 'off' ? 'Fra' : `${cl.state === 'heat' ? 'Varme' : cl.state}${cl.attributes.temperature != null ? ' · mål ' + fmt(num(cl.attributes.temperature)) : ''}`;
          g += PILL(150, by + 80, 236, 34, ctx, sc.climate) + PILL(150, by + 126, 236, 34, `el ${fmt(sc.el_power ? kW(h, sc.el_power) * 1000 : null, 0)} W`, sc.el_power) + TX(150, by + 170, this.airPlan(h, s), 'm c');
          const ys = blockAt(s);
          if (ys.length) { g += PATH(`M280 ${by + 40} H305 V${Math.max(...ys)}`, 'air'); for (const yy of ys) g += PATH(`M305 ${yy} H${rx - 2}`, 'air', true); }
          y = Math.max(y, by + 220);
        }
        g += this.legend(20, y + 10, 3, air.length) + TX(20, y + 80, 'Tryk på en værdi for detaljer og historik', 'm');
        return { w: 760, h: y + 100, g };
      }
      if (main) for (const rw of block(main, 58)) if (rw.d.valve && rw.d.valve !== 'altid åben') g += PATH(`M1100 ${rw.y} H1148`, rw.d.vs === 'on' ? 'hot' : 'idle');
      for (const s of air) {
        y = Math.max(y, 600);
        const by = y - 6; const sc = this.sc(s.name);
        const rows = block(s, 48);
        g += BOX(900, by, 200, 170, s.name, sc.model || 'luft-luft');
        const cl = sc.climate ? st(h, sc.climate) : null;
        const ctx = !cl ? '–' : cl.state === 'off' ? 'Fra' : `${cl.state === 'heat' ? 'Varme' : cl.state}${cl.attributes.temperature != null ? ' · mål ' + fmt(num(cl.attributes.temperature)) : ''}`;
        g += PILL(1000, by + 76, 184, 34, ctx, sc.climate) + PILL(1000, by + 120, 184, 34, `el ${fmt(sc.el_power ? kW(h, sc.el_power) * 1000 : null, 0)} W`, sc.el_power) + TX(1000, by + 156, this.airPlan(h, s), 'm c');
        if (rows.length) { const ys = rows.map((r) => r.y); g += PATH(`M1100 ${by + 40} H1125 V${Math.max(...ys)}`, 'air'); for (const yy of ys) g += PATH(`M1125 ${yy} H1148`, 'air', true); }
      }
      const H = Math.max(main ? 680 : 840, y + 10);
      g += this.legend(300, Math.max(main ? 590 : 740, H - 90), 4, air.length) + TX(300, Math.max(main ? 650 : 806, H - 30), 'Tryk på en værdi for detaljer og historik', 'm');
      return { w: 1500, h: H, g };
    }
    _setK(k) { this._k = +k || 0; this._draw(); }
    // Styrer Varmemodel? En luft-luft-kilde siger det selv (kontakten »Varmemodel styrer«); ellers kortets dry_run
    dry(h, vm) {
      const air = vm.sources.length === 1 && vm.sources[0].kind !== 'hydronic' ? vm.sources[0] : null;
      const hyd = vm.sources.find((x) => x.kind === 'hydronic');
      const hs = hyd ? (st(h, hyd.e('hydro_control')) || {}).state === 'on' : false;
      const sw = air ? st(h, air.e('air_control')) : hyd ? (hs ? st(h, hyd.e('hydro_control')) : st(h, hyd.e('flow_control'))) : null;
      return sw ? sw.state !== 'on' : !!this._c.dry_run;
    }
    // Timeforbruget (kWh el) fra statistikken – hentes højst hvert 10. minut
    kwh(h, eid) {
      const c = this._kw && this._kw.eid === eid ? this._kw : null;
      if (eid && !this._kwBusy && (!c || Date.now() - c.t > 600e3)) {
        this._kwBusy = true;
        const start = new Date(); start.setHours(0, 0, 0, 0); start.setDate(start.getDate() - 7);
        h.callWS({ type: 'recorder/statistics_during_period', start_time: start.toISOString(), statistic_ids: [eid], period: 'hour', types: ['change'], units: { energy: 'kWh' } })
          .then((r) => { this._kw = { eid, t: Date.now(), d: (r[eid] || []).map((x) => ({ t: typeof x.start === 'number' ? x.start : Date.parse(x.start), v: x.change })).filter((x) => x.v !== null && x.v >= 0) }; })
          .catch(() => { this._kw = { eid, t: Date.now(), d: [] }; })
          .finally(() => { this._kwBusy = false; this._draw(); });
      }
      return c ? c.d : null;
    }
    // Rumtemperaturer de sidste 12 timer (til kurverne) – hentes højst hvert 10. minut
    hist(h, eids) {
      const key = eids.join(','); const c = this._hi && this._hi.key === key ? this._hi : null;
      if (eids.length && !this._hiBusy && (!c || Date.now() - c.t > 600e3)) {
        this._hiBusy = true;
        h.callWS({ type: 'history/history_during_period', start_time: new Date(Date.now() - 12 * 3600e3).toISOString(), entity_ids: eids, minimal_response: true, no_attributes: true, significant_changes_only: false })
          .then((r) => { const d = {}; for (const e of eids) d[e] = (r[e] || []).map((x) => [((x.lu ?? x.lc) || 0) * 1000, num(x.s)]).filter((x) => x[1] !== null); this._hi = { key, t: Date.now(), d }; })
          .catch(() => { this._hi = { key, t: Date.now(), d: {} }; })
          .finally(() => { this._hiBusy = false; this._draw(); });
      }
      return c ? c.d : {};
    }
    // Bento-plan: rummene som kort, placeret som på plantegningen (rækker efter indhold, ikke efter cm).
    // Hvert kort: temperatur, kurve (12 t målt + forudsigelse stiplet), gulv og retning. Farven = afstand til gulv/loft.
    bento(h, vm, f, k, o = {}) {
      // Uden plantegning: kildens rum som kort i et gitter (virker uden opsætning)
      if (!f || !(f.rooms || []).length) {
        const units = o.unitEntity ? [{ entity: o.unitEntity, name: o.unitName }] : [];
        f = { rooms: vm.rooms.map((r, i) => ({ name: r.name, rect: [i % 2 * 100, Math.floor(i / 2) * 100, 100, 100] })), units };
        if (units.length && f.rooms.length) { units[0].x = 50; units[0].y = 50; }
      }
      const named = (f.rooms || []).filter((rr) => !rr.room && vm.rooms.find((x) => x.name === rr.name));
      const box = (rr) => { const p = pts(rr); const xs = p.map((q) => q[0]), ys = p.map((q) => q[1]); return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]; };
      const bx = named.map(box);
      const X = [...new Set(bx.flatMap((b) => [b[0], b[2]]))].sort((a, b) => a - b), Y = [...new Set(bx.flatMap((b) => [b[1], b[3]]))].sort((a, b) => a - b);
      const cols = X.slice(1).map((x, i) => `${Math.max(1, x - X[i])}fr`).join(' ');
      // varmepumpen hører til det rum, dens punkt ligger i (også en del af et rum)
      const inside = (rr, x, y) => { const [a, b, c2, d] = box(rr); return x >= a && x <= c2 && y >= b && y <= d; };
      const unitRoom = {};
      for (const u of f.units || []) { if (!u.entity) continue; const hit = (f.rooms || []).find((rr) => inside(rr, u.x, u.y)); if (hit) unitRoom[hit.room || hit.name] = u; }
      const ds = named.map((rr) => room(h, vm, vm.rooms.find((x) => x.name === rr.name)));
      const hist = this.hist(h, ds.map((d) => d.tE).filter(Boolean));
      const UT = { heat: 'varmer', idle: 'pause', defrost: 'afrimer', off: 'slukket' };
      const tiles = named.map((rr, i) => {
        const d = ds[i], a = d.a, b = bx[i];
        const lo = num(a.gulv), hi = num(a.loft);
        const t = k === 0 ? d.t : (d.fc && d.fc.length >= k ? d.fc[k - 1] : null);
        const col = HCOL(t, lo, hi);
        // kurve: målt 12 t + forudsigelse 6 t
        const now = Date.now(), t0 = now - 12 * 3600e3, t1 = now + 6 * 3600e3;
        const ms = (hist[d.tE] || []).filter((x) => x[0] >= t0);
        const nowH = new Date(); nowH.setMinutes(0, 0, 0);
        const fs = (d.fc || []).slice(0, 6).map((v, j) => [nowH.getTime() + (j + 1) * 3600e3, v]);
        if (d.t !== null) fs.unshift([now, d.t]);
        const all = [...ms, ...fs].map((x) => x[1]).concat(lo !== null ? [lo] : []);
        let spark = '';
        if (all.length > 1) {
          const vmin = Math.min(...all) - 0.2, vmax = Math.max(...all) + 0.2, W = 200, HH = 44;
          const px = (x) => ((x - t0) / (t1 - t0)) * W, py = (v) => HH - ((v - vmin) / (vmax - vmin)) * HH;
          const line = (arr) => arr.map((x, j) => `${j ? 'L' : 'M'}${px(x[0]).toFixed(1)} ${py(x[1]).toFixed(1)}`).join(' ');
          spark = `<svg class="sp" viewBox="0 0 ${W} ${HH}" preserveAspectRatio="none">`
            + (lo !== null ? `<line x1="0" x2="${W}" y1="${py(lo).toFixed(1)}" y2="${py(lo).toFixed(1)}" class="spf"/>` : '')
            + `<line x1="${px(now).toFixed(1)}" x2="${px(now).toFixed(1)}" y1="0" y2="${HH}" class="spn"/>`
            + (ms.length > 1 ? `<path d="${line(ms)}" class="spm" style="stroke:${col}"/>` : '')
            + (fs.length > 1 ? `<path d="${line(fs)}" class="spc" style="stroke:${col}"/>` : '') + '</svg>';
        }
        const gE = a.min_entitet || d.r.model; const gv = a.min_entitet ? sv(h, a.min_entitet) : lo;
        const f3 = d.fc && d.fc.length > 2 ? d.fc[2] : null; const tr = f3 !== null && d.t !== null ? (f3 - d.t > 0.15 ? '↗' : f3 - d.t < -0.15 ? '↘' : '→') : '';
        const u = unitRoom[rr.name]; const live = u && o.unitEntity === u.entity ? o.unit : null;
        const chip = u ? `<span class="uchip ${live || ''}" data-e="${esc(u.entity)}"><span class="fan${live === 'heat' ? ' spin' : ''}">✻</span>${esc(o.unitName || u.name || '')} · ${UT[live] || '–'}</span>` : '';
        const ci = X.indexOf(b[0]) + 1, ce = X.indexOf(b[2]) + 1, ri = Y.indexOf(b[1]) + 1, re = Y.indexOf(b[3]) + 1;
        return `<div class="bt" style="grid-column:${ci}/${ce};grid-row:${ri}/${re};--c:${col}">`
          + `<div class="bh"><span class="bn" data-e="${esc(d.tE || d.r.model)}">${esc(rr.label || rr.name)}</span>${chip}</div>`
          + `<div class="bv" data-e="${esc(d.tE || d.r.model)}">${t === null ? '–' : `${k === 0 && d.est ? '≈' : ''}${fmt(t)}°`}${k ? `<span class="mut sm"> om ${k} t</span>` : ''}</div>`
          + spark
          + `<div class="bf">${a.i_plan !== false && gv !== null ? `<span class="gl2" data-e="${esc(gE)}">gulv ${fmt(gv)} ✎</span>` : '<span></span>'}`
          + `${f3 !== null ? `<span>${tr} ${fmt(f3)}° om 3 t</span>` : ''}</div></div>`;
      }).join('');
      return `<div class="bento" style="grid-template-columns:${cols}">${tiles}</div>`;
    }
    // Luft-luft på sin etage: varmepumpen, kWh el over tid og trinene til venstre – etagen som heatmap til højre
    airView(h, vm, s) {
      const c = this._c, k = this._k || 0;
      // Klimaentitet og eleffekt: fra kortets opsætning, ellers fra integrationen (plug and play)
      const sc = { climate: at(h, s.e('air_step')).klima || undefined, ...this.sc(s.name) };
      const cl = sc.climate ? st(h, sc.climate) : null; const pa = at(h, s.plan);
      const elW = sc.el_power ? kW(h, sc.el_power) : null;
      const running = elW !== null ? elW * 1000 >= (sc.running_w ?? 150) : cl ? (cl.attributes.hvac_action ? cl.attributes.hvac_action === 'heating' : cl.state === 'heat') : false;
      const unit = !cl || cl.state === 'off' ? 'off' : (sc.defrost && isOn(h, sc.defrost)) ? 'defrost' : running ? 'heat' : 'idle';
      const UT = { heat: 'varmer', idle: 'pause', defrost: '❄ afrimer', off: 'slukket' };
      // varmepumpen
      let L = `<div class="b pvb"><div class="pvh">${esc(s.name)}<span class="mut sm"> · ${esc(sc.model || 'luft-luft')}</span></div>`
        + `<div class="pvs"><span class="dot" style="background:${unit === 'heat' ? OR : unit === 'defrost' ? BL : '#9aa0a6'}"></span><span class="v" data-e="${esc(sc.climate || '')}">${UT[unit]}`
        + `${cl && cl.attributes.temperature != null ? ` · setpunkt ${fmt(num(cl.attributes.temperature))} °C` : ''}${cl && cl.attributes.fan_mode ? ` · blæser ${esc(cl.attributes.fan_mode)}` : ''}</span></div>`
        + `<div class="sm" style="margin:2px 0 6px">${esc(this.airPlan(h, s))}</div>`;
      const stats = (c.stats || []).map((x) => {
        const v = x.attribute ? at(h, x.entity)[x.attribute] : (st(h, x.entity) || {}).state;
        const nv = num(v); const txt = v === undefined || v === null ? '–' : v === 'on' ? 'ja' : v === 'off' ? 'nej' : nv !== null && x.digits !== undefined ? fmt(nv, x.digits) : String(v);
        return `<div>${esc(x.name || x.entity)}</div><div><span class="v" data-e="${esc(x.entity)}">${esc(txt)}${x.unit ? ' ' + esc(x.unit) : ''}</span></div>`;
      }).join('');
      if (stats) L += `<div class="grid">${stats}</div>`;
      L += '</div>';
      // Ventilationen, der fordeler varmen (valgt i kildens opsætning): tilstand og hastighed kan ændres her
      const va = at(h, s.e('air_step'));
      if (va.ventilation_entitet) {
        const ctl = (eid) => {
          const s0 = st(h, eid); if (!s0) return '–'; const a2 = s0.attributes || {}; const dom = eid.split('.')[0];
          if (dom === 'select' || dom === 'input_select') return `<select data-svc="${dom}.select_option" data-eid="${esc(eid)}">${(a2.options || []).map((x) => `<option${x === s0.state ? ' selected' : ''}>${esc(x)}</option>`).join('')}</select>`;
          if (dom === 'number' || dom === 'input_number') return `<input type="number" data-svc="${dom}.set_value" data-eid="${esc(eid)}" value="${esc(s0.state)}" min="${a2.min}" max="${a2.max}" step="${a2.step}">`;
          if (['fan', 'switch', 'input_boolean'].includes(dom)) return `<button class="tg2${s0.state === 'on' ? ' on' : ''}" data-act="${esc(eid)}">${s0.state === 'on' ? 'til' : 'fra'}</button>`;
          return `<span class="v" data-e="${esc(eid)}">${esc(s0.state)}</span>`;
        };
        L += `<div class="b"><div class="pvh">Ventilation<span class="mut sm"> · fordeler varmen · ${va.ventilation === 'til' ? 'kører' : 'står'}</span></div>`
          + `<div class="sr"><span>Tilstand</span><span>${ctl(va.ventilation_entitet)}</span></div>`
          + (va.ventilation_hastighed_entitet ? `<div class="sr"><span>Hastighed</span><span>${ctl(va.ventilation_hastighed_entitet)}</span></div>` : '')
          + `<div class="sm" style="margin-top:4px">${va.ventilation_virkning_pct != null ? `Med ventilation ${va.ventilation_virkning_pct >= 0 ? '+' : ''}${fmt(va.ventilation_virkning_pct, 0)} % varme i rummene pr. kWh el` : 'Effekten på varmen pr. kWh el læres (samme trin med og uden ventilation)'}</div>`
          + (c.assist_automation ? `<div class="sm mut">Styres af <span class="v" data-e="${esc(c.assist_automation)}">${esc(at(h, c.assist_automation).friendly_name || c.assist_automation)}</span> (${(st(h, c.assist_automation) || {}).state === 'on' ? 'til' : 'fra'})</div>` : '') + '</div>';
      }
      // kWh el over tid: sidste 24 timer (farvet efter pris) og planen de næste 12 timer, dagene under
      const eid = sensorsOf(h, s).electricity_energy; const rows = this.kwh(h, eid);
      const P = {};
      for (const o of this._all || []) { const oa = at(h, o.plan); for (const x of [...(oa.history || []), ...(oa.plan || [])]) P[Date.parse(x.t)] = { price: x.price, blocked: x.blocked }; }
      for (const x of pa.history || []) P[Date.parse(x.t)] = { ...P[Date.parse(x.t)], ...x }; for (const x of pa.plan || []) P[Date.parse(x.t)] = { ...P[Date.parse(x.t)], ...x };
      const prs = Object.values(P).map((x) => x.price).filter((x) => x != null).sort((a, b) => a - b);
      const q1 = prs[Math.floor(prs.length / 3)], q2 = prs[Math.floor((2 * prs.length) / 3)];
      const pc = (x) => (!x ? '#9aa0a6' : x.blocked ? RE : x.price <= q1 ? GR : x.price <= q2 ? YE : OR);
      const nowH = new Date(); nowH.setMinutes(0, 0, 0); const nk = nowH.getTime();
      const got = {}; for (const r of rows || []) got[r.t] = r.v;
      const slots = []; for (let i = -23; i <= 12; i++) { const t = nk + i * 3600e3; slots.push({ t, past: i <= 0, v: i <= 0 ? got[t] ?? null : (P[t] ? (P[t].el_kw ?? P[t].heat_kw ?? 0) : null), p: P[t] }); }
      const mx = Math.max(0.3, ...slots.map((x) => x.v || 0));
      const W = 420, HB = 90, bw = W / slots.length;
      let svg = `<svg viewBox="0 0 ${W} ${HB + 20}" class="kw">`;
      slots.forEach((x, i) => {
        if (x.v === null) return; const ht = Math.max(1, (HB * x.v) / mx); const col = pc(x.p);
        svg += `<rect x="${(i * bw + 1).toFixed(1)}" y="${(HB - ht).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${ht.toFixed(1)}" rx="1.5" fill="${x.past ? col : 'none'}" stroke="${col}" stroke-width="${x.past ? 0 : 1.5}"${x.past ? '' : ' stroke-dasharray="3 2"'}><title>${hh(new Date(x.t).getHours())}:00 · ${fmt(x.v, 2)} kWh el${x.past ? '' : ' (plan)'}${x.p && x.p.price != null ? ` · ${fmt(x.p.price, 2)} kr/kWh el` : ''}</title></rect>`;
      });
      svg += `<line x1="${(24 * bw).toFixed(1)}" y1="0" x2="${(24 * bw).toFixed(1)}" y2="${HB}" stroke="var(--primary-text-color)" stroke-width="1" stroke-dasharray="2 2"/>`;
      for (const i of [0, 6, 12, 18, 24, 30]) svg += `<text x="${(i * bw).toFixed(1)}" y="${HB + 14}">${hh(new Date(nk + (i - 23) * 3600e3).getHours())}</text>`;
      svg += '</svg>';
      const d0 = new Date(); d0.setHours(0, 0, 0, 0); const t0 = d0.getTime();
      const today = rows ? rows.filter((r) => r.t >= t0).reduce((a, r) => a + r.v, 0) : null;
      const days = []; for (let i = 6; i >= 0; i--) { const a = t0 - i * 864e5; days.push({ a, v: rows ? rows.filter((r) => r.t >= a && r.t < a + 864e5).reduce((x, r) => x + r.v, 0) : null }); }
      const dm = Math.max(0.5, ...days.map((x) => x.v || 0));
      const dbar = days.map((x, i) => `<div class="db"><i style="height:${Math.round((48 * (x.v || 0)) / dm)}px${i === 6 ? ';background:' + OR : ''}"></i><span>${fmt(x.v, 1)}</span><span class="mut">${i === 6 ? 'i dag' : new Date(x.a).toLocaleDateString('da-DK', { weekday: 'short' }).replace('.', '')}</span></div>`).join('');
      let K = '', S = '';
      K += `<div class="b"><div class="pvh">kWh el<span class="mut sm"> · pr. time, 24 t tilbage og planen 12 t frem</span></div>${rows ? svg : '<div class="mut sm">henter …</div>'}`
        + `<div class="sm"><span class="sw" style="background:${GR}"></span>billig <span class="sw" style="background:${YE}"></span>middel <span class="sw" style="background:${OR}"></span>dyr <span class="sw" style="background:${RE}"></span>rød · stiplet = plan</div>`
        + `<div class="sm" style="margin-top:6px">I dag <b>${fmt(today, 1)} kWh el</b> · i går ${M(h, s.e('el_yesterday'), 1, 'kWh el')} · snit 7 døgn ${M(h, s.e('el_week'), 1, 'kWh el')}</div>`
        + `<div class="dbs">${dbar}</div></div>`;
      // trinene: hvad hvert trin gør og hvad det lærte trin bruger
      const ase = s.e('air_step'); const asa = at(h, ase);
      if (asa.trin_liste) {
        const cur = num(asa.trin); const L2 = asa.trin_liste; const wmx = Math.max(100, ...L2.map((x) => num(x.w) || 0));
        const eb = Math.max(0, ...L2.map((x) => num(x.grader_pr_kwh_el) || 0));
        let tr = '';
        for (const x of [...L2].reverse()) {
          const w = num(x.w);
          tr += `<div class="tr${x.trin === cur ? ' on' : ''}${x.trin === asa.mål_trin ? ' mt' : ''}"><span class="tn">${x.trin}</span><span class="tl2">${esc(x.blæser || '')} · ${fmt(x.setpunkt, 1)} °C</span>`
            + `<span class="tbar"><i style="width:${w !== null ? Math.round((100 * w) / wmx) : 0}%"></i></span><span class="tw">${w !== null ? `${fmt(w, 0)} W · ${fmt(w / 1000, 2)} kWh el/t` : '<span class="mut">ikke lært</span>'}${num(x.grader_pr_kwh_el) !== null && eb > 0 ? ` · <b title="rummenes varme pr. kWh el i forhold til det bedste trin">${fmt((100 * x.grader_pr_kwh_el) / eb, 0)} %</b>` : ''}</span></div>`;
        }
        S += `<div class="b"><div class="pvh">Stigen<span class="mut sm"> · ${asa.nat ? 'nat' : 'dag'}, maks. blæser ${esc(asa.maks_blæser || '–')} · nu trin ${cur ?? '–'}${asa.el_mål_w ? ` · planens el-mål ${fmt(asa.el_mål_w, 0)} W${asa.mål_trin != null ? ` (trin ${asa.mål_trin})` : ' (lærer stadig trinene)'}` : ''}${asa.kører_nu ? ` · kører ${esc(asa.kører_nu)}` : ''}</span></div>`
          + `<div class="sm" style="margin-bottom:4px">${asa.ventilation ? `Ventilation ${esc(asa.ventilation)}${asa.ventilation_virkning_pct != null ? ` · med ventilation ${asa.ventilation_virkning_pct >= 0 ? '+' : ''}${fmt(asa.ventilation_virkning_pct, 0)} % varme pr. kWh el` : ' · effekten læres'} · ` : ''}${esc(asa.grund || '')}${asa.styrer ? '' : ' <span class="mut">(anbefaling – Varmemodel styrer ikke)</span>'}</div><div class="trs" data-e="${esc(ase)}">${tr}</div><div class="sm mut" style="margin-top:4px"><b style="color:var(--info-color,#3b82c4)">Fyldt blå</b> = trinnet nu · <b>stiplet ramme</b> = planens mål-trin · <span style="color:#f2711c">orange bjælke</span> = lært forbrug (W). % = varme i rummene pr. kWh el i forhold til det bedste trin (lært af rumfølerne, efter afkøling og opdrift fra stueplanen). Stigen vælger det trin, der dækker planens varme for mindst el; med rolig opstart prøves et trin i 30 min., og der skrues kun op, når det kolde rum ikke stiger 0,2 °C/t.</div></div>`;
      }
      L += settingsPanel(h, s);
      const R = `<div class="sm" style="margin-bottom:6px">${(c.hours || [0, 3, 6, 12]).map((x) => `<span class="pill${x === k ? ' on' : ''}" data-k="${x}">${x === 0 ? 'nu' : `om ${x} t`}</span>`).join(' ')}</div>`
        + this.bento(h, vm, c.floor, k, { unit, unitEntity: sc.climate, unitName: s.name })
        + `<div class="sm mut bleg" style="margin-top:8px"><span class="grad"></span><span>under gulvet</span><span>godt</span><span>loftet</span> · kurve: 12 t målt, stiplet = forudsigelse · ✎ ret gulvet</div>`;
      // Venstre: varmepumpen, ventilationen og indstillingerne. Højre: etagen og under den kWh og stigen side om side
      return `<div class="pv"><div class="pvl">${L}</div><div class="pvr">${R}<div class="pv2">${K}${S}</div></div></div>`;
    }
    // Historik til fremløbsgrafen (12 t): målt fremløb og retur, kompressoren og målet i varmepumpen – hentes højst hvert 5. min.
    flowHist(h, main, f) {
      const key = main.id, now = Date.now(); this._fhc = this._fhc || {};
      const c = this._fhc[key];
      if ((!c || now - c.t > 300000) && !(c && c.loading)) {
        const ids = [f.flow_temp, f.return_temp, f.compressor, f.flow_target].filter(Boolean);
        this._fhc[key] = { ...(c || {}), t: c ? c.t : 0, loading: true };
        if (ids.length && h.callWS) h.callWS({ type: 'history/history_during_period', start_time: new Date(now - 12 * 3600000).toISOString(),
          entity_ids: ids, minimal_response: false, no_attributes: false, significant_changes_only: false })
          .then((r) => { this._fhc[key] = { t: Date.now(), data: r || {}, loading: false }; if (this._draw) this._draw(); })
          .catch(() => { this._fhc[key] = { t: Date.now(), data: {}, loading: false }; });
      }
      return (this._fhc[key] || {}).data || {};
    }
    // Graf 12 t tilbage og 12 t frem: målt fremløb (ud) og retur (ind), målet som trappe, planens timer frem, loftet,
    // kompressor i drift (bjælke forneden) og blokerede timer (rød baggrund)
    flowChart(h, main, fs, f) {
      const hist = this.flowHist(h, main, f);
      const now = Date.now(), t0 = now - 12 * 3600000, t1 = now + 12 * 3600000;
      const ser = (eid, attr) => (hist[eid] || []).map((x) => ({ t: (x.lu ?? x.lc ?? 0) * 1000, v: attr ? num((x.a || {})[attr]) : num(x.s), s: x.s }));
      const ud = ser(f.flow_temp).filter((x) => x.v !== null), ind = ser(f.return_temp).filter((x) => x.v !== null);
      const tg = f.flow_target ? ser(f.flow_target, 'temperature').filter((x) => x.v !== null) : [];
      const comp = ser(f.compressor);
      const pa = at(h, main.plan); const fut = (pa.plan || []).map((x) => ({ t: Date.parse(x.t), v: x.heat_kw > 0 ? num(x.flow) : null, b: !!x.blocked }));
      const past = (pa.history || []).map((x) => ({ t: Date.parse(x.t), b: !!x.blocked }));
      const vals = [...ud, ...ind, ...tg].map((x) => x.v).concat(fut.map((x) => x.v), [fs.loft, fs.mål]).filter((x) => x !== null && x !== undefined);
      const lo = Math.floor(Math.min(20, ...vals) / 2) * 2, hi = Math.ceil(Math.max(34, ...vals) / 2) * 2;
      const W = 640, H = 190, L = 30, R = 10, T = 8, B = 30;
      const X = (t) => L + ((Math.min(Math.max(t, t0), t1) - t0) / (t1 - t0)) * (W - L - R), Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
      const hhmm = (t) => { const d = new Date(t); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
      let g = '';
      // Blokerede timer (rød), fortid og fremtid
      for (const x of [...past, ...fut]) if (x.b && x.t + 3600000 > t0 && x.t < t1) g += `<rect x="${X(x.t)}" y="${T}" width="${Math.max(0, X(x.t + 3600000) - X(x.t))}" height="${H - T - B}" fill="#d6453d" opacity="0.12"/>`;
      for (let v = lo; v <= hi; v += 2) g += `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--divider-color,#888)" stroke-width="0.5"/>` + (v % 4 === 0 ? `<text x="${L - 4}" y="${Y(v) + 4}" text-anchor="end" class="ax">${v}</text>` : '');
      const h0 = Math.ceil(t0 / 3600000) * 3600000;
      for (let t = h0; t <= t1; t += 3 * 3600000) g += `<line x1="${X(t)}" x2="${X(t)}" y1="${T}" y2="${H - B}" stroke="var(--divider-color,#888)" stroke-width="0.3"/><text x="${X(t)}" y="${H - 8}" text-anchor="middle" class="ax">${hhmm(t)}</text>`;
      // Kompressor i drift: bjælke forneden
      let on = null;
      for (const x of [...comp, { t: now, s: 'end' }]) { if (on !== null && x.t > on) { g += `<rect x="${X(on)}" y="${H - B + 2}" width="${Math.max(1, X(x.t) - X(on))}" height="5" fill="${OR}" opacity="0.8"/>`; } on = x.s === 'on' ? Math.max(x.t, t0) : null; }
      const line = (pts, col, w, dash) => { const p2 = pts.filter((x) => x.t >= t0 - 3600000); if (p2.length < 1) return ''; return `<polyline points="${p2.map((x) => `${X(x.t)},${Y(x.v)}`).join(' ')} ${X(now)},${Y(p2[p2.length - 1].v)}" fill="none" stroke="${col}" stroke-width="${w}"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`; };
      const step = (pts, col, w, dash, end) => { if (!pts.length) return ''; let d = ''; let last = null; for (const x of pts) { const xx = X(x.t), yy = Y(x.v); d += last === null ? `M${xx},${yy}` : `H${xx}V${yy}`; last = yy; } d += `H${X(end)}`; return `<path d="${d}" fill="none" stroke="${col}" stroke-width="${w}"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`; };
      g += line(ind, BL, 1.5) + line(ud, OR, 1.5);
      g += step(tg.filter((x) => x.t >= t0 - 12 * 3600000), 'var(--primary-text-color,#ddd)', 2.2, null, now);
      // Planens timer frem (fremløb i varmetimerne)
      let fp = ''; for (const x of fut) if (x.v !== null && x.t + 3600000 > now) fp += `<line x1="${X(Math.max(x.t, now))}" x2="${X(x.t + 3600000)}" y1="${Y(x.v)}" y2="${Y(x.v)}" stroke="var(--secondary-text-color,#999)" stroke-width="2.2" stroke-dasharray="4 3"/>`;
      g += fp;
      if (fs.loft != null) g += `<line x1="${L}" x2="${W - R}" y1="${Y(fs.loft)}" y2="${Y(fs.loft)}" stroke="var(--secondary-text-color,#999)" stroke-width="1" stroke-dasharray="1 3"/><text x="${W - R - 2}" y="${Y(fs.loft) - 3}" text-anchor="end" class="ax">loft ${fmt(fs.loft, 0)}</text>`;
      g += `<line x1="${X(now)}" x2="${X(now)}" y1="${T}" y2="${H - B}" stroke="var(--primary-text-color,#ddd)" stroke-width="1" stroke-dasharray="2 2"/><text x="${X(now) + 3}" y="${T + 10}" class="ax">nu</text>`;
      const loading = !Object.keys(hist).length;
      return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;margin-top:6px" role="img" aria-label="Fremløb: målt, mål og planens timer 12 timer tilbage og frem">${g}</svg>`
        + `<div class="sm mut">${loading ? 'henter historik … · ' : ''}<span style="color:${OR}">━</span> målt fremløb (ud) · <span style="color:${BL}">━</span> retur (ind) · <b>━</b> mål i varmepumpen · ╌ planens timer frem · ┄ loft · <span style="color:${OR}">▬</span> kompressor kører · <span style="color:#d6453d">▮</span> blokeret</div>`;
    }
    // Søg laveste fremløb: status i klart sprog, mål/målt/loft, graf, lært pr. udetemperatur og »Sådan virker det«
    flowPanel(h, main) {
      if (!main) return '';
      const set = settingsPanel(h, main, 'Indstillinger');
      const ca = at(h, main.e('command')); const fs = ca.flow_search;
      if (!fs) return set ? `<div class="pv2">${set}</div>` : '';
      const f = sensorsOf(h, main);
      const SC = { forvarmer: ['forvarmer', '#d6453d'], holder: ['holder', '#2e9e5b'], hæver: ['hæver', '#e0702a'], sænker: ['sænker', '#3b8ad8'], løfter: ['løfter', '#e0a02a'], start: ['starter', '#e0a02a'], slukket: ['slukket', '#8a9198'] };
      const [stxt, scol] = SC[fs.forvarmer && !fs.slukket ? 'forvarmer' : fs.status] || [fs.status || '–', '#8a9198'];
      const chip = `<span class="pill" style="background:${scol};color:#fff;border-color:${scol}">${esc(stxt)}</span>`;
      const ud = sv(h, f.flow_temp), ind = sv(h, f.return_temp);
      const n3 = (lbl, v, sub) => `<div class="fsc"><div class="mut sm">${lbl}</div><div class="fsv">${v}</div>${sub ? `<div class="mut sm">${sub}</div>` : ''}</div>`;
      const nums = `<div class="fsr">${n3('Mål i varmepumpen', `${fmt(fs.mål_nu ?? fs.mål, 0)} °C`, fs.slukket ? 'ved næste start' : fs.forvarmer ? `forvarmer +${fmt(fs.forvarmer, 0)} °C (søgning ${fmt(fs.mål, 0)})` : 'søger laveste')}`
        + `${n3('Målt ud / ind', `${fmt(ud, 1)} / ${fmt(ind, 1)} °C`, ud !== null && ind !== null ? `ΔT ${fmt(ud - ind, 1)} K` : '')}`
        + `${n3('Loft', `${fmt(fs.loft, 0)} °C`, fs.mål != null && fs.loft != null && fs.loft > fs.mål ? `${fmt(fs.loft - fs.mål, 0)} °C over målet` : 'det lærte fremløb + margen')}</div>`;
      const lært = Object.entries(fs.lært || {}).sort((a, b) => +a[0] - +b[0]).map(([k, v]) => `<tr><td>${String(+k).replace('-', '−')}…${String(+k + 5).replace('-', '−')} °C ude</td><td class="n"><b>${fmt(v, 0)} °C</b></td></tr>`).join('');
      const box = `<div class="b"><div class="pvh">Fremløb · søg laveste ${chip}</div>`
        + `<div style="margin:4px 0 6px;font-size:14px">${esc(fs.tekst || '')}</div>` + nums + this.flowChart(h, main, fs, f)
        + `<div class="sm" style="margin-top:6px"><b>Lært laveste fremløb</b> ${lært ? `<div class="wrap"><table>${lært}</table></div>` : '<span class="mut">– endnu intet (læres, når målet har holdt alle rum over gulvet i 3 timer)</span>'}</div>`
        + `<details id="fsHow"${this._open && this._open.has('fsHow') ? ' open' : ''}><summary class="sm">Sådan virker det</summary><div class="sm mut" style="margin-top:4px">`
        + `Luft-vand starter ved retur + 3 °C og holder målet lavt. Planen regner med samme mål, så forudsigelser og »ventet« passer med det, der kører, og den lægger flere timer med lavt fremløb.<br>`
        + `• +1 °C pr. time, når et rum er på vej under gulvet inden for 3 timer (målt tendens over 2 timer)<br>• +2 °C pr. halve time, når et rum er under gulvet og ikke på vej op<br>`
        + `• +1 °C, når kompressoren stopper (målet er under varmepumpens laveste ydelse)<br>• −1 °C hver anden time, når alle rum er mindst 0,5 °C over gulvet og stabile<br>`
        + `• forvarmning: kan flere timer ved søgningens mål ikke holde rummene gennem en blokering, hæver planen fremløbet (højst 5 °C) – vist som »forvarmer«<br>`
        + `• aldrig over loftet (det lærte fremløb + margen). Rummet, der bestemmer, er markeret med ◆ i rumlisten; pilen viser rummets tendens.</div></details></div>`;
      return `<div class="pv2">${box}${set}</div>`;
    }
    airPlan(h, s) {
      const ps = st(h, s.plan); if (!ps) return 'plan: –';
      const pa = ps.attributes || {}; const k = ps.state;
      let t = `plan: ${PS[k] || k}`;
      if (k === 'heat') t += ` ${fmt(pa.el_kw ?? pa.heat_kw)} kW el` + (pa.next_off ? ` · stop ${when(pa.next_off)}` : '');
      else if (pa.next_on) t += ` · start ${when(pa.next_on)}`;
      const pt = preText(h, s, true);
      return pt ? `${t} · ${pt}` : t;
    }
    tallSvg(h, vm, main, air, D) {
      const c = this._c, p = this.pills(h, D, main); let g = '';
      const P = (x, y, w, v, ht = 34) => (v ? PILL(x, y, w, ht, v[0], v[1], v[2] || {}) : '');
      const cols = [{ n: 'ude nu', e: D.outdoor }, ...(c.extra || []).slice(0, 2).map((x) => ({ n: x.name, e: x.entity }))];
      const vv = main && (D.f.dhw || p.tank); const uw = vv ? 245 : 500;
      g += BOX(10, 10, uw, 100, cols.length > 1 ? 'Ude og andre' : 'Ude');
      cols.forEach((x, i) => { const cx = Math.round(10 + (uw * (i + 0.5)) / cols.length); g += TX(cx, 56, x.n, 'm c') + PILL(cx, 84, Math.min(110, uw / cols.length - 8), 32, `${fmt(sv(h, x.e))} °C`, x.e); });
      let y = 125;
      if (main) {
        const sc = this.sc(main.name);
        if (vv) g += BOX(265, 10, 245, 100, 'Varmt vand') + TX(327, 56, 'beholder', 'm c') + TX(447, 56, 'laver nu', 'm c') + (p.tank ? P(327, 84, 105, p.tank) : '') + P(447, 84, 110, p.vv);
        g += BOX(10, 125, 500, 235, `${main.name} · ${sc.model || 'varmepumpe'}`);
        g += P(135, 180, 230, p.status) + TX(135, 212, p.plan, 'm c', D.differs ? ` style="fill:${YE};font-weight:700"` : '');
        g += P(385, 180, 230, p.ud) + P(135, 250, 230, p.cop) + P(385, 250, 230, p.flow) + P(385, 320, 230, p.ind);
        const bp = !!c.bypass, bf = !!c.buffer;
        g += PATH('M440 360 V776', 'hot', true) + PATH('M80 780 V364', 'cold', true);
        if (bp) {
          g += PATH(bf ? 'M427 450 H300 V560 H152' : 'M427 450 H93', 'ovl', true, true) + TEE(440, 450) + TX(424, 482, c.bypass_name || 'kortslutning', 'm r');
          g += TX(bf ? 170 : 200, bf ? 632 : 440, 'fremløb ↔ retur', 'm');
        }
        if (p.pump) g += '<rect x="370" y="500" width="140" height="70" rx="35" class="comp"/>' + TX(450, 530, sc.pump_name || 'Pumpe', 'ttl') + P(440, 554, 112, p.pump, 24);
        if (bf) g += '<rect x="10" y="520" width="140" height="80" rx="8" class="comp"/>' + TX(80, 556, sc.buffer_name || 'Buffer', 'ttl') + TX(80, 578, 'i returen', 'm c');
        if (p.a) g += SEN(440, 405, 'A') + TX(320, 381, 'VP ud', 'm c') + P(330, 405, 100, p.a);
        if (p.b) g += SEN(440, 715, 'B') + TX(320, 691, 'manifold ind', 'm c') + P(330, 715, 100, p.b);
        if (p.d) g += SEN(80, 420, 'D') + TX(195, 396, 'VP ind', 'm c') + P(185, 420, 100, p.d);
        if (p.c) g += SEN(80, 720, 'C') + TX(195, 696, 'manifold retur', 'm c') + P(185, 720, 100, p.c);
        g += BOX(10, 780, 500, 120, `${sc.manifold_name || 'Manifold'} · gulvvarme`);
        g += P(95, 846, 160, p.dthus) + P(260, 846, 150, p.ab) + P(425, 846, 150, p.dc);
        if (p.ab || p.dc) g += TX(260, 886, p.balTxt, 'm c', D.bal ? ` style="fill:${GR};font-weight:700"` : '');
        g += BOX(10, 915, 500, 110, 'Energi i dag') + TX(95, 952, 'kWh varme', 'm c') + TX(260, 952, 'kWh el', 'm c') + TX(425, 952, 'COP', 'm c');
        g += P(95, 978, 140, p.vdag, 30) + P(260, 978, 120, p.edag, 30) + P(425, 978, 100, p.copd, 30) + TX(260, 1012, p.igaar, 'm c');
        y = 1060;
      }
      const block = (s) => {
        const ds = s.rooms.map((r) => room(h, vm, r)); const chk = ds.filter((d) => d.chk); const hit = chk.filter((d) => d.status === 'ramt').length;
        g += TX(18, y, `${s.name}${chk.length ? ` · ramt ${hit}/${chk.length}` : ''}`, 'hdr'); y += 14;
        let i = 0;
        for (const d of ds) { g += this.roomRow(h, vm, d, 10 + (i % 2) * 255, y + Math.floor(i / 2) * 98, 245, 90, true); i++; }
        y += Math.ceil(ds.length / 2) * 98 + 30;
      };
      if (main) block(main);
      for (const s of air) {
        const sc = this.sc(s.name); const cl = sc.climate ? st(h, sc.climate) : null;
        g += BOX(10, y, 500, 120, `${s.name} · ${sc.model || 'luft-luft'}`);
        const ctx = !cl ? '–' : cl.state === 'off' ? 'Fra' : `${cl.state === 'heat' ? 'Varme' : cl.state}${cl.attributes.temperature != null ? ' · mål ' + fmt(num(cl.attributes.temperature)) : ''}`;
        g += PILL(135, y + 70, 230, 34, ctx, sc.climate) + PILL(385, y + 70, 230, 34, `el ${fmt(sc.el_power ? kW(h, sc.el_power) * 1000 : null, 0)} W`, sc.el_power) + TX(260, y + 106, this.airPlan(h, s), 'm c');
        y += 150; block(s);
      }
      g += this.legend(10, y, 3, air.length) + TX(10, y + 70, 'Tryk på en værdi for detaljer og historik', 'm');
      return { w: 520, h: y + 90, g };
    }
  }

  const DEFS = [
    ['varmemodel-anlaeg-card', AnlaegCard, 'Varmemodel: anlæg', 'Anlægget nu, planen og afvigelser – med eller uden buffer og kortslutning'],
    ['varmemodel-rum-card', RumCard, 'Varmemodel: rum', 'Rummene: målt, forudsagt og om forudsigelsen holdt'],
    ['varmemodel-kilde-card', KildeCard, 'Varmemodel: varmekilder', 'Effektivitet, bygning og anlæg for hver varmekilde'],
    ['varmemodel-tiltag-card', TiltagCard, 'Varmemodel: energitiltag', 'Afkøling, vaner, vind og før/efter for tiltag'],
    ['varmemodel-hus-card', HusCard, 'Varmemodel: hus', 'Status, plan og indstillinger'],
    ['varmemodel-plan-card', PlanCard, 'Varmemodel: plantegning', 'Rummene på din plantegning farvet efter temperatur – nu og de næste timer'],
  ];
  // ------------------------------------------------------------------ dashboard på ét klik
  // strategy: {type: custom:varmemodel} – et dashboard bygget af det, integrationen kender: én fane pr. varmekilde
  // (anlæg + indstillinger), rum, varmekilder, energitiltag og hus. »Tag kontrol« i HA gør det til almindelig YAML.
  class VarmemodelStrategy {
    static async generate(config, hass) {
      const vm = discover(hass);
      if (!vm) return { title: 'Varme', views: [{ title: 'Varmemodel', cards: [{ type: 'markdown', content: 'Varmemodel er ikke sat op endnu.' }] }] };
      const ov = (config && config.sources) || {};
      const slug = (n) => n.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'kilde';
      const views = vm.sources.map((s) => ({
        title: s.name, path: slug(s.name), icon: s.kind === 'hydronic' ? 'mdi:water-boiler' : 'mdi:fan', type: 'panel',
        cards: [{ type: 'custom:varmemodel-anlaeg-card', source: s.name, ...(config && config.anlaeg || {}), ...(ov[s.name] || {}) }],
      }));
      views.push(
        { title: 'Rum', path: 'rum', icon: 'mdi:home-thermometer', type: 'panel', cards: [{ type: 'custom:varmemodel-rum-card' }] },
        { title: 'Varmekilder', path: 'varmekilder', icon: 'mdi:heat-pump', type: 'sections', max_columns: 2,
          sections: vm.sources.map((s) => ({ type: 'grid', cards: [{ type: 'custom:varmemodel-kilde-card', source: s.name }] })) },
        { title: 'Energitiltag', path: 'energitiltag', icon: 'mdi:leaf', type: 'panel', cards: [{ type: 'custom:varmemodel-tiltag-card' }] },
        { title: 'Hus', path: 'hus', icon: 'mdi:home-cog', type: 'sections', sections: [{ type: 'grid', cards: [{ type: 'custom:varmemodel-hus-card' }] }] },
      );
      return { title: (config && config.title) || 'Varme', views };
    }
  }
  if (!customElements.get('ll-strategy-dashboard-varmemodel')) customElements.define('ll-strategy-dashboard-varmemodel', class extends HTMLElement { static generate(c, h) { return VarmemodelStrategy.generate(c, h); } });

  window.customCards = window.customCards || [];
  for (const [tag, cls, name, description] of DEFS) {
    if (!customElements.get(tag)) { customElements.define(tag, cls); window.customCards.push({ type: tag, name, description, preview: false }); }
  }
  console.info(`%c VARMEMODEL-KORT %c ${VERSION} `, 'background:#e8833a;color:#fff;font-weight:700', '');
})();
