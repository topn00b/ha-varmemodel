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
  const VERSION = '0.5.12';

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
  const GR = '#2e9e5b', YE = '#d29b12', RE = '#d6453d', BL = '#3b82c4', OR = '#f2711c', ORT = '#e8890c';
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
        return { id: d.id, name: name(d), kind: plan ? 'hydronic' : 'air_air', plan, rooms, e: (k) => tk(d.id, k) };
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
          const nav = path.find((x) => x.dataset && x.dataset.nav);
          if (nav) { history.pushState(null, '', nav.dataset.nav); window.dispatchEvent(new CustomEvent('location-changed')); return; }
          const t = path.find((x) => x.dataset && x.dataset.e);
          if (t) { ev.stopPropagation(); this.dispatchEvent(new CustomEvent('hass-more-info', { detail: { entityId: t.dataset.e }, bubbles: true, composed: true })); }
        });
        r.addEventListener('toggle', (ev) => { const d = ev.target; if (d && d.id) { if (d.open) this._open.add(d.id); else this._open.delete(d.id); } }, true);
      }
      const vm = discover(this._h);
      let body;
      try { body = vm ? this.body(this._h, vm) : '<div class="mut">Varmemodel er ikke sat op endnu.</div>'; }
      catch (e) { body = `<div class="red">Fejl i kortet: ${esc(e && e.message)}</div>`; }
      this.shadowRoot.innerHTML = `<style>${CSS}${this.css || ''}</style><ha-card>${this._c.title ? `<div class="h">${esc(this._c.title)}</div>` : ''}${body}</ha-card>`;
      for (const id of this._open) { const d = this.shadowRoot.getElementById(id); if (d) d.open = true; }
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
          + (compact ? '' : '<th class="n">Laveste ventet</th><th class="n">Afkøling °C/t ved 20 K</th><th class="n">Tidskonstant</th><th class="n">°C pr. kWh</th><th class="n">Varme pr. døgn</th><th class="n">Præcision 6 t</th><th>Telestat</th>') + '</tr>';
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
      const row = (k, v) => `<div>${k}</div><div class="n">${v}</div>`;
      let o = '<div class="h2">Effektivitet</div><div class="grid">';
      o += row('COP i går', `${V(h, y, 2)} <span class="mut sm">${fmt(ya.varme_kwh, 0)} kWh varme / ${fmt(ya.el_kwh, 1)} kWh el · ude ${fmt(ya.ude_snit)} °C</span>`);
      o += row('– uden varmt vand / forventet', `${A(h, y, ya.cop_uden_varmt_vand, 2)} / ${A(h, sd, ya.forventet_stabil_cop, 2)} <span class="mut sm">(${sgn(num(ya.forskel_fra_stabil_pct))} %)</span>`);
      o += row('COP 7 døgn', V(h, s.e('cop_week'), 2));
      o += row('Stabil drift (0 °C ude, 35 °C fremløb, 4 kW)', V(h, sd, 2));
      o += row('Timemodel (0 °C ude, 35 °C fremløb)', V(h, s.e('cop'), 2));
      o += row('Pr. grad lavere fremløb', A(h, sd, sa.per_k_flow_pct, 1, '% bedre COP'));
      o += row('Tab fra opstart og afrimning', V(h, s.e('cop_loss'), 1, '%'));
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
      o += row('Varmekurve ved 0 °C ude', V(h, s.e('curve'), 1, '°C'));
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
          + `<tr><td>1 °C lavere inde</td><td class="n">${fmt(k1)} kWh</td><td class="n">${fmt(k1 / cop)} kWh</td><td class="n">${pris ? 'ca. ' + fmt((k1 / cop) * pris * 30, 0) + ' kr' : '–'}</td></tr>`
          + `<tr><td>10 % mindre varmetab</td><td class="n">${fmt(k10)} kWh</td><td class="n">${fmt(k10 / cop)} kWh</td><td class="n">${pris ? 'ca. ' + fmt((k10 / cop) * pris * 30, 0) + ' kr' : '–'}</td></tr></table></div>`
          + `<div class="leg">Varmetab ${V(h, s.e('heat_loss'), 0, 'W/K')} · COP ${fmt(cop, 2)} (7 døgn) · elpris ${fmt(pris, 2)} kr/kWh (planens snit) · ${fmt(ude)} °C ude (i går).</div>`;
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
        o += `<div class="wrap"><table><tr><th>Tiltag</th><th>Dato</th><th class="n">Varmetab før</th><th class="n">Efter</th><th class="n">Ændring</th><th>Data</th></tr>`;
        for (const m of list) {
          o += `<tr><td>${esc(m.navn)}</td><td>${esc(m.dato)}</td><td class="n">${A(h, ms, m['før_w_k'], 1, 'W/K')}</td><td class="n">${A(h, ms, m.efter_w_k, 1, 'W/K')}</td>`
            + `<td class="n ${m['ændring_pct'] < 0 ? 'green' : m['ændring_pct'] > 0 ? 'red' : ''}">${m['ændring_pct'] == null ? '–' : sgn(m['ændring_pct']) + ' %'}</td>`
            + `<td class="mut sm">${m.dage_før} døgn før · ${m.dage_efter} efter${m.note ? ' · ' + esc(m.note) : ''}</td></tr>`;
        }
        if (!list.length) o += '<tr><td colspan="6" class="mut">Ingen tiltag i kalenderen endnu.</td></tr>';
        o += `</table></div><div class="leg">Tilføj et tiltag som en begivenhed i kalenderen <button class="lnk" data-nav="/calendar">Åbn kalenderen</button>. Varmetabet regnes for sig før og efter datoen (kræver ca. 16 døgn på hver side) ved næste nats kalibrering.</div>`;
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
        if (ce) o += row('Anbefalet styring', `<span class="v" data-e="${esc(ce)}">${(st(h, ce) || {}).state === 'heat' ? 'varme' : 'slukket'}${ca.shift != null ? ' · kurve ' + sgn(ca.shift, 0) : ''}</span> <span class="mut sm">${esc(ca.reason || '')}</span>`);
        if (ce) o += row('I dag', `${ca.writes_today ?? 0} skrivninger · ${ca.switches_today ?? 0} tænd/sluk ${ca.within_limits === false ? '<span class="amber">over grænsen</span>' : '<span class="green">inden for grænsen</span>'}`);
        o += row('Frisk plan i dag', `${fmt(pa.fresh_pct_today, 0)} % af kvartererne`);
        o += '</div>';
      }
      o += '<div class="h2">Planens indstillinger</div><div class="grid">';
      for (const [k, n, u] of [['max_pause', 'Maks. pause uden gulvvarme', 't'], ['comfort_offset', 'Komfort over komfortgulvet', '°C'], ['boost_delta', 'Jeg fryser hæver med', '°C'],
        ['boost_hours', 'Jeg fryser gælder højst', 't'], ['red_allowance', 'Må falde i blokerede timer', '°C']]) {
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
      this.css = ACSS;
      this._ro = new ResizeObserver(() => { const w = this.clientWidth; if (w && (w < 700) !== this._tall) { this._tall = w < 700; this._draw(); } });
    }
    connectedCallback() { this._ro.observe(this); }
    disconnectedCallback() { this._ro.disconnect(); }
    sc(name) { return (this._c.sources || {})[name] || {}; }
    body(h, vm) {
      const main = vm.sources.find((s) => s.kind === 'hydronic') || null;
      const air = vm.sources.filter((s) => s.kind !== 'hydronic');
      const D = this.data(h, vm, main);
      this.today(h, D);
      return this.band(h, vm, main, D) + this.svg(h, vm, main, air, D);
    }
    data(h, vm, main) {
      const D = { outdoor: (at(h, vm.status)['følere'] || {}).ude || null, f: {}, cfg: {}, pa: {}, pst: 'unknown' };
      if (!main) return D;
      const f = sensorsOf(h, main), cfg = this.sc(main.name), pa = at(h, main.plan);
      const pst = (st(h, main.plan) || {}).state || 'unknown';
      const flow = sv(h, f.flow_temp), ret = sv(h, f.return_temp);
      const comp = st(h, f.compressor); const compOn = !!comp && comp.state === 'on';
      const onMin = compOn ? (Date.now() - Date.parse(comp.last_changed)) / 60e3 : 0;
      const defr = isOn(h, f.defrost), dhw = isOn(h, f.dhw);
      let shift = null;
      if (f.curve_offset) {
        const o = f.curve_offset_attribute ? num(at(h, f.curve_offset)[f.curve_offset_attribute]) : sv(h, f.curve_offset);
        if (o !== null) shift = Math.round(o - (num(f.curve_offset_zero) || 0));
      }
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
      const differs = pst !== 'unknown' && (planHeat !== actHeat || (planHeat && actHeat && shift !== null && pa.shift != null && Math.abs(shift - pa.shift) >= 1));
      const pumpOn = cfg.pump_running ? isOn(h, cfg.pump_running) : null;
      const bal = compOn && onMin >= 15 && !defr && !dhw && pumpOn !== false;
      Object.assign(D, { f, cfg, pa, pst, flow, ret, compOn, onMin, defr, dhw, shift, clim, hpk, ep, steady, live, exp, lowMin, differs, pumpOn, bal,
        A_: sv(h, cfg.a), B_: sv(h, cfg.b), C_: sv(h, cfg.c), D_: sv(h, cfg.d) });
      return D;
    }
    today(h, D) {
      const ids = [D.f.heat_energy, D.f.electricity_energy].filter(Boolean);
      if (!ids.length || (this._tT && Date.now() - this._tT < 300e3)) return;
      this._tT = Date.now();
      const d0 = new Date(); d0.setHours(0, 0, 0, 0);
      h.callWS({ type: 'recorder/statistics_during_period', start_time: d0.toISOString(), period: 'day', statistic_ids: ids, types: ['change'] })
        .then((r) => { this._today = {}; for (const id of ids) this._today[id] = (r[id] || []).reduce((s, x) => s + (x.change || 0), 0); this._draw(); })
        .catch(() => {});
    }
    band(h, vm, main, D) {
      const c = this._c, pa = D.pa, pst = D.pst;
      const z = c.zone ? (st(h, c.zone) || {}).state || '' : ''; const zl = z.toLowerCase();
      const zc = !z ? null : zl.startsWith('r') ? RE : zl.startsWith('gu') || zl.startsWith('y') ? YE : zl.startsWith('g') ? GR : '#6b7278';
      const ico = zc === RE ? '🔴' : zc === YE ? '🟡' : zc === GR ? '🟢' : '';
      const price = c.price ? sv(h, c.price) : num(pa.price);
      let plan = main ? `${PS[pst] || pst}` : 'ingen vandbåren varmekilde';
      if (pst === 'heat') plan += ` ${fmt(pa.heat_kw)} kW` + (pa.shift != null ? ` · kurve ${sgn(pa.shift, 0)}` : '') + (pa.next_off ? ` · stop ${when(pa.next_off)}` : '');
      else if (pa.next_on) plan += ` · start ${when(pa.next_on)}`;
      if (pa.boost) plan += ' · jeg fryser' + (pa.boost_until && pa.boost_until !== 'udløbet' ? ` til ${when(pa.boost_until)}` : '');
      const flags = (c.flags || []).filter((x) => (st(h, x.entity) || {}).state === String(x.state ?? 'on')).map((x) => x.text || x.entity);
      const ce = main && main.e('command'); const cs = st(h, ce);
      if (cs && cs.state !== 'unknown' && (cs.state === 'heat') !== (pst === 'heat')) plan += ` · styring: ${cs.state === 'heat' ? 'varme' : 'slukket'} (${cs.attributes.reason || ''})`;
      const bg = zc || 'var(--primary-color)';
      const zone = `<div class="b zb" style="background:${bg};border-color:${bg}"><div class="z">${z ? `${ico} ${esc(z.toUpperCase())} · ` : ''}${fmt(price, 2)} kr/kWh</div>`
        + `<div class="sub">Plan: ${esc(plan)}${c.dry_run ? ' (styrer ikke endnu)' : ''}${flags.length ? ' · ' + esc(flags.join(' · ')) : ''}</div></div>`;
      const notes = (c.notes || []).map((n) => { const v = n.attribute ? at(h, n.entity)[n.attribute] : (st(h, n.entity) || {}).state; return v ? `<div>${esc(v)}</div>` : ''; }).join('');
      const acts = (c.actions || []).map((a) => {
        const s = st(h, a.entity); const on = !!s && s.state === 'on';
        return `<button class="${on ? 'on' : ''}" data-act="${esc(a.entity)}">${esc(a.name || a.entity)}${on ? ' · siden ' + new Date(s.last_changed).toLocaleTimeString('da-DK', { hour: '2-digit', minute: '2-digit' }) : ''}</button>`;
      }).join('');
      return `<div class="band">${zone}<div class="b zn">${this.timeline(h, vm)}</div><div class="b afv">${this.alerts(h, vm, D)}</div>`
        + `${notes ? `<div class="b note">${notes}</div>` : ''}${acts ? `<div class="acts">${acts}</div>` : ''}</div>`;
    }
    timeline(h, vm) {
      const hyd = vm.sources.filter((s) => s.kind === 'hydronic'); const air = vm.sources.filter((s) => s.kind !== 'hydronic');
      if (!hyd.length) return '<span class="mut">Ingen plan (ingen vandbåren varmekilde)</span>';
      const pa0 = at(h, hyd[0].plan);
      const P = {};
      for (const p of pa0.history || []) P[Date.parse(p.t)] = { price: p.price, blocked: p.blocked };
      for (const p of pa0.plan || []) P[Date.parse(p.t)] = p;
      const keys = Object.keys(P).map(Number);
      if (!keys.length) return '<span class="mut">Ingen plan endnu</span>';
      const pr = keys.map((k) => P[k].price).filter((x) => x != null).sort((a, b) => a - b);
      const q1 = pr[Math.floor(pr.length / 3)], q2 = pr[Math.floor((2 * pr.length) / 3)];
      const HM = hyd.map((s) => {
        const a = at(h, s.plan); const m = {};
        for (const p of a.history || []) m[Date.parse(p.t)] = p.plan;
        for (const p of a.plan || []) m[Date.parse(p.t)] = p.heat_kw;
        return { s, m, mx: Math.max(1, ...Object.values(m).filter((x) => x != null)) };
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
          return `<i class="${cl}" style="background:${col}" title="${hh(i)}:00 · ${p ? fmt(p.price, 2) + ' kr/kWh' + (p.blocked ? ' · blokeret' : '') : 'ingen pris'}"></i>`;
        })}</div></div>`;
        for (const x of HM) {
          rows += `<div class="zl"><span class="mut sm">↳ ${esc(x.s.name)}</span><div class="zr v">${cells(day, (k, i, cl) => {
            const v = x.m[k];
            return `<i class="${cl}" style="background:${v ? OR : 'var(--divider-color)'};${v ? `opacity:${(0.35 + (0.65 * v) / x.mx).toFixed(2)}` : ''}" title="${hh(i)}:00 · ${v ? fmt(v) + ' kW varme' : 'ingen varme'}"></i>`;
          })}</div></div>`;
        }
      }
      for (const s of air) rows += `<div class="zl"><span class="mut sm">↳ ${esc(s.name)}</span><span class="mut sm">ingen plan endnu</span></div>`;
      rows += `<div class="zl"><span></span><div class="zt">${[0, 6, 12, 18, 24].map((x) => `<span style="left:${(x / 24) * 100}%">${hh(x)}</span>`).join('')}</div></div>`;
      const pa = pa0;
      return rows + `<details id="vmzd"><summary>planens begrundelse ▾</summary><div class="sm" style="margin-top:4px">${esc(pa.reason || '')}<br>`
        + `Behov ${fmt(pa.demand_kwh, 0)} kWh · varme ${fmt(pa.heat_kwh, 0)} kWh · el ${fmt(pa.el_kwh, 0)} kWh · ca. ${fmt(pa.cost_kr, 0)} kr over ${pa.horizon_h ?? '–'} timer<br>`
        + `Komfort +${fmt(pa.comfort_offset)} °C · må falde ${fmt(pa.red_allowance)} °C i blokerede timer · maks. pause ${pa.max_pause ?? '–'} timer<br>`
        + (() => { const ca = at(h, hyd[0].e('command')); return ca.reason !== undefined ? `Anbefalet styring: ${esc(ca.reason)} · i dag ${ca.writes_today ?? 0} skrivninger (højst 20) og ${ca.switches_today ?? 0} tænd/sluk (højst 8)<br>` : ''; })()
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
        if ((a.rooms_held || []).length) w.push(['⚠️', `${s.name}: bruger sidst kendte temperatur for ${a.rooms_held.join(', ')} (føleren svarer ikke)`]);
        for (const [r, e] of Object.entries(a.rooms_estimated || {})) w.push(['❗', `Mangler data: ${r} har ikke målt siden ${whenM(e['sidste_måling'])} (${e.minutter} min.) – planen bruger forudsigelsen ≈${fmt(e['forudsagt'])} °C. Tjek føleren.`]);
        if ((a.rooms_skipped || []).length) w.push(['❗', `${s.name}: ${a.rooms_skipped.join(', ')} er ikke med i planen (føler eller rummodel mangler)`]);
        const ca = at(h, s.e('command')); if (ca.within_limits === false) w.push(['⚠️', `${s.name}: styringen har i dag ${ca.writes_today} skrivninger og ${ca.switches_today} tænd/sluk (grænse 20 og 8)`]);
        for (const [r, k] of Object.entries(a.rooms_check || {})) if (k.flag) w.push(['❗', `${r}: ${fmt(k['målt'])} °C, ventet ${fmt(k.ventet)} °C – åben dør eller vindue?`]);
      }
      if (D.lowMin >= 30) w.push(['⚠️', `COP ${fmt(D.live, 2)} mod forventet ${fmt(D.exp, 2)} i ${Math.round(D.lowMin)} min.`]);
      if (D.differs && !this._c.dry_run) w.push(['⚠️', 'Varmepumpen følger ikke planen']);
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
      if (D.shift !== null && !(D.clim && D.clim.state === 'off')) s0 += ` · kurve ${sgn(D.shift, 0)}`;
      p.status = [s0, cfg.climate || f.compressor, { bg }];
      p.plan = `plan: ${PS[D.pst] || D.pst}${D.pst === 'heat' && pa.shift != null ? ' · kurve ' + sgn(pa.shift, 0) : ''}`;
      p.ud = [`ud ${fmt(D.flow)} °C${D.pst === 'heat' && pa.flow != null ? ' · mål ' + fmt(pa.flow) : ''}`, f.flow_temp];
      p.ind = [`ind ${fmt(D.ret)} °C`, f.return_temp];
      p.cop = [D.steady ? `${fmt(D.hpk)} kW · COP ${fmt(D.live)} / ${fmt(D.exp)}` : `${fmt(D.hpk)} kW · COP –`, cfg.heat_power || f.heat_energy, { bg: D.lowMin >= 30 ? YE : null }];
      const fr = sv(h, cfg.flow_rate);
      p.flow = [`${fr === null ? '–' : fmt(fr)} L/min · ΔT ${D.flow !== null && D.ret !== null ? fmt(D.flow - D.ret) : '–'} K`, cfg.flow_rate || f.flow_temp];
      p.tank = cfg.tank ? [`${fmt(sv(h, cfg.tank))} °C`, cfg.tank] : null;
      p.vv = [D.dhw ? 'laver varmt vand' : 'nej', f.dhw];
      const T = this._today || {}; const ht = T[f.heat_energy], et = T[f.electricity_energy];
      p.vdag = [`${fmt(ht)} kWh`, f.heat_energy]; p.edag = [`${fmt(et)} kWh`, f.electricity_energy];
      p.copd = [ht && et ? fmt(ht / et, 2) : '–', main.e('cop_yesterday')];
      const ya = at(h, main.e('cop_yesterday'));
      p.igaar = `i går u. varmt vand ${fmt(ya.cop_uden_varmt_vand, 2)} · forventet ${fmt(ya.forventet_stabil_cop, 2)}`;
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
        const txt = D.pumpOn === false ? 'står' : (D.pumpOn ? 'kører' : '') + (k ? (D.pumpOn ? ' · ' : '') + 'kurve ' + k : ps ? ' ' + ps : '');
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
      const sub1 = d.passive ? (() => { const pl = pulls(h, vm, d.r, d.t); return pl ? `${pl.eff >= 0 ? 'varmer' : 'køler'} ${pl.name} ${fmt(Math.abs(pl.eff), 2)} °C/t` : 'opvarmes passivt'; })()
        : d.valve ? (d.valve === 'altid åben' ? 'altid åben' : d.vs === 'on' ? 'telestat åben' : d.vs === 'off' ? 'telestat lukket' : '') : '';
      const sub2 = d.chk ? `ventet ${fmt(d.chk.ventet)} · ${statusTxt(d)}` : 'ingen forudsigelse endnu';
      const fc = d.f3 !== null ? [`om 3 t ${fmt(d.f3)}`, `om 6 t ${fmt(d.f6)}`] : [];
      if (!tall) {
        const two = !!sub1;
        g += TX(x + 22, y + (two ? 20 : 24), d.r.name, 'rn', nameEx);
        if (flag) g += TX(x + 30 + d.r.name.length * 9.5, y + (two ? 20 : 24), '!', 'flg', nameEx);
        if (sub1) g += TX(x + 22, y + 37, sub1, 'vt');
        g += TX(x + 22, y + (two ? 51 : 41), sub2, 'vt', col ? ` style="fill:${col}"` : '');
        g += TX(x + 215, y + ht / 2 + 8, tTxt(d), 'rt c', ` data-e="${esc(d.tE)}"${d.est ? ` style="fill:${YE}"` : col ? ` style="fill:${col}"` : ''}`);
        fc.forEach((s, i) => { g += TX(x + w - 12, y + ht / 2 - 4 + i * 17, s, 'vt r', nameEx); });
      } else {
        g += TX(x + 16, y + 26, d.r.name, 'rn', nameEx);
        if (flag) g += TX(x + 24 + d.r.name.length * 9.5, y + 26, '!', 'flg', nameEx);
        if (sub1) g += TX(x + w - 12, y + 26, sub1.replace('telestat ', ''), 'vt r');
        g += TX(x + 16, y + 58, tTxt(d), 'rt', ` data-e="${esc(d.tE)}"${d.est ? ` style="fill:${YE}"` : col ? ` style="fill:${col}"` : ''}`);
        if (fc.length) g += TX(x + w - 12, y + 56, `${fmt(d.f3)} → ${fmt(d.f6)}`, 'vt r', nameEx);
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
      g += BOX(20, 20, 260, 100, cols.length > 1 ? 'Ude og andre steder' : 'Ude');
      cols.forEach((x, i) => { const cx = Math.round(20 + (260 * (i + 0.5)) / cols.length); g += TX(cx, 64, x.n, 'm c') + PILL(cx, 92, 230 / cols.length, 32, `${fmt(sv(h, x.e))} °C`, x.e); });
      if (main) {
        const sc = this.sc(main.name);
        g += BOX(20, 140, 260, 420, main.name, sc.model || 'varmepumpe');
        g += P(150, 200, 236, p.status) + TX(150, 232, p.plan, 'm c', D.differs ? ` style="fill:${YE};font-weight:700"` : '');
        g += P(150, 260, 236, p.ud) + P(150, 345, 236, p.cop) + P(150, 430, 236, p.flow);
        if (D.f.dhw || p.tank) {
          g += BOX(20, 580, 260, 120, 'Varmt vand') + TX(90, 628, 'beholder', 'm c') + TX(215, 628, 'laver nu', 'm c');
          g += (p.tank ? P(90, 656, 110, p.tank) : TX(90, 662, '–', 'c')) + P(215, 656, 130, p.vv) + PATH('M150 560 V580', D.dhw ? 'hot' : 'idle');
        }
        g += BOX(20, 716, 260, 110, 'Energi i dag') + TX(66, 746, 'varme', 'm c') + TX(150, 746, 'el', 'm c') + TX(234, 746, 'COP', 'm c');
        g += P(66, 772, 82, p.vdag, 30) + P(150, 772, 82, p.edag, 30) + P(234, 772, 74, p.copd, 30) + TX(150, 812, p.igaar, 'm c');
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
      let y = 136;
      const block = (s, rowH) => {
        const ds = s.rooms.map((r) => room(h, vm, r)); const chk = ds.filter((d) => d.chk); const hit = chk.filter((d) => d.status === 'ramt').length;
        g += TX(1150, y, `${s.name}${chk.length ? ` · forudsigelse ramt ${hit}/${chk.length}` : ''}`, 'hdr'); y += 14;
        const rows = [];
        for (const d of ds) { const ht = d.passive ? 44 : rowH; g += this.roomRow(h, vm, d, 1150, y, 340, ht, false); rows.push({ d, y: y + ht / 2 }); y += ht + 8; }
        y += 26; return rows;
      };
      if (main) for (const rw of block(main, 58)) if (rw.d.valve && rw.d.valve !== 'altid åben') g += PATH(`M1100 ${rw.y} H1148`, rw.d.vs === 'on' ? 'hot' : 'idle');
      for (const s of air) {
        y = Math.max(y, 600);
        const by = y - 6; const sc = this.sc(s.name);
        const rows = block(s, 48);
        g += BOX(900, by, 200, 170, s.name, sc.model || 'luft-luft');
        const cl = sc.climate ? st(h, sc.climate) : null;
        const ctx = !cl ? '–' : cl.state === 'off' ? 'Fra' : `${cl.state === 'heat' ? 'Varme' : cl.state}${cl.attributes.temperature != null ? ' · mål ' + fmt(num(cl.attributes.temperature)) : ''}`;
        g += PILL(1000, by + 76, 184, 34, ctx, sc.climate) + PILL(1000, by + 120, 184, 34, `el ${fmt(sc.el_power ? kW(h, sc.el_power) * 1000 : null, 0)} W`, sc.el_power) + TX(1000, by + 156, 'plan: kommer', 'm c');
        if (rows.length) { const ys = rows.map((r) => r.y); g += PATH(`M1100 ${by + 40} H1125 V${Math.max(...ys)}`, 'air'); for (const yy of ys) g += PATH(`M1125 ${yy} H1148`, 'air', true); }
      }
      const H = Math.max(840, y + 10);
      g += this.legend(300, Math.max(740, H - 90), 4, air.length) + TX(300, Math.max(806, H - 30), 'Tryk på en værdi for detaljer og historik', 'm');
      return { w: 1500, h: H, g };
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
        g += P(385, 180, 230, p.ud) + P(135, 250, 230, p.cop) + P(385, 250, 230, p.flow);
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
        g += BOX(10, 915, 500, 110, 'Energi i dag') + TX(95, 952, 'varme', 'm c') + TX(260, 952, 'el', 'm c') + TX(425, 952, 'COP', 'm c');
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
        g += PILL(135, y + 70, 230, 34, ctx, sc.climate) + PILL(385, y + 70, 230, 34, `el ${fmt(sc.el_power ? kW(h, sc.el_power) * 1000 : null, 0)} W`, sc.el_power) + TX(260, y + 106, 'plan: kommer', 'm c');
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
  ];
  window.customCards = window.customCards || [];
  for (const [tag, cls, name, description] of DEFS) {
    if (!customElements.get(tag)) { customElements.define(tag, cls); window.customCards.push({ type: tag, name, description, preview: false }); }
  }
  console.info(`%c VARMEMODEL-KORT %c ${VERSION} `, 'background:#e8833a;color:#fff;font-weight:700', '');
})();
