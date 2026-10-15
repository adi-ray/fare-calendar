/**
 * <fare-calendar> — drop-in date picker that shows the estimated lowest fare
 * under every date.
 *
 *   <script src="https://YOUR-HOST/widget/fare-calendar.js"></script>
 *   <fare-calendar api="https://YOUR-HOST" origin="DEL" destination="BOM"></fare-calendar>
 *
 * Fires `fare-select` with { date, fare, source } when an agent picks a date.
 * Attributes: api, origin, destination, month (YYYY-MM, defaults to this month),
 *             api-key (sent as X-Api-Key when the service is protected; prefer proxying via your backend).
 */
(() => {
  const css = `
  :host { --fc-bg:#fff; --fc-ink:#1f2328; --fc-muted:#6b7280; --fc-line:#e5e7eb;
          --fc-low:#15803d; --fc-mid:#b45309; --fc-high:#b91c1c; --fc-sel:#1d4ed8;
          display:block; font:14px/1.3 system-ui,-apple-system,"Segoe UI",sans-serif;
          color:var(--fc-ink); background:var(--fc-bg); border:1px solid var(--fc-line);
          border-radius:12px; padding:12px; max-width:460px; }
  @media (prefers-color-scheme: dark) {
    :host { --fc-bg:#1b1e22; --fc-ink:#e8e6e3; --fc-muted:#9ca3af; --fc-line:#30353b;
            --fc-low:#4ade80; --fc-mid:#fbbf24; --fc-high:#f87171; --fc-sel:#60a5fa; }
  }
  header { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
  header strong { font-size:15px; }
  button.nav { border:1px solid var(--fc-line); background:transparent; color:inherit;
               border-radius:8px; width:32px; height:32px; cursor:pointer; font-size:16px; }
  .grid { display:grid; grid-template-columns:repeat(7,1fr); gap:4px; }
  .dow { text-align:center; color:var(--fc-muted); font-size:12px; padding:2px 0; }
  .day { border:1px solid var(--fc-line); border-radius:8px; padding:6px 2px; min-height:52px;
         background:transparent; color:inherit; cursor:pointer; display:flex; flex-direction:column;
         align-items:center; gap:2px; font:inherit; }
  .day:disabled { opacity:.35; cursor:default; }
  .day .n { font-weight:600; }
  .day .f { font-size:12px; font-variant-numeric:tabular-nums; }
  .day .s { font-size:10px; color:var(--fc-muted); }
  .day.low .f { color:var(--fc-low); } .day.mid .f { color:var(--fc-mid); } .day.high .f { color:var(--fc-high); }
  .day.cheapest { border-color:var(--fc-low); border-width:2px; }
  .day[aria-pressed="true"] { outline:2px solid var(--fc-sel); outline-offset:1px; }
  .day:focus-visible { outline:2px solid var(--fc-sel); }
  footer { margin-top:8px; color:var(--fc-muted); font-size:12px; display:flex; flex-wrap:wrap; gap:4px 12px; }
  .msg { padding:16px; text-align:center; color:var(--fc-muted); }`;

  const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  class FareCalendar extends HTMLElement {
    static get observedAttributes() { return ['origin', 'destination', 'month', 'api', 'api-key']; }

    constructor() {
      super();
      this.root = this.attachShadow({ mode: 'open' });
      this.selected = null;
      this.cache = new Map();
    }

    connectedCallback() { this.load(); }
    attributeChangedCallback() { if (this.isConnected) this.load(); }

    get month() {
      const m = this.getAttribute('month');
      if (m && /^\d{4}-\d{2}$/.test(m)) return m;
      const d = new Date();
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    }

    shift(delta) {
      const [y, m] = this.month.split('-').map(Number);
      const d = new Date(y, m - 1 + delta, 1);
      this.setAttribute('month', `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`);
    }

    async load() {
      const origin = this.getAttribute('origin');
      const destination = this.getAttribute('destination');
      if (!origin || !destination) return this.paint(null, 'Choose origin and destination to see fares.');
      const api = (this.getAttribute('api') || '').replace(/\/$/, '');
      const url = `${api}/api/calendar?origin=${encodeURIComponent(origin)}&destination=${encodeURIComponent(destination)}&month=${this.month}`;
      this.paint(null, 'Loading fares…');
      try {
        if (!this.cache.has(url)) {
          const key = this.getAttribute('api-key');
          const res = await fetch(url, key ? { headers: { 'X-Api-Key': key } } : {});
          const body = await res.json();
          if (!res.ok) throw new Error(body.detail || 'Could not load fares');
          this.cache.set(url, body);
        }
        this.paint(this.cache.get(url));
      } catch (e) {
        this.cache.delete(url);
        this.paint(null, e.message);
      }
    }

    paint(data, message) {
      const [y, m] = this.month.split('-').map(Number);
      const title = new Date(y, m - 1, 1).toLocaleString(undefined, { month: 'long', year: 'numeric' });
      const fmt = (n, cur) => new Intl.NumberFormat(undefined, { style: 'currency', currency: cur, maximumFractionDigits: 0 }).format(n);
      const sym = (cur) => new Intl.NumberFormat(undefined, { style: 'currency', currency: cur, currencyDisplay: 'narrowSymbol' })
        .formatToParts(0).find((p) => p.type === 'currency')?.value || '';
      const short = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));

      let body = `<div class="msg">${message || ''}</div>`;
      if (data) {
        const lead = (new Date(y, m - 1, 1).getDay() + 6) % 7; // Monday-first offset
        const cells = Array.from({ length: lead }, () => '<span></span>');
        for (const d of data.days) {
          const n = Number(d.date.slice(8));
          const cls = ['day', d.tier || '', d.cheapest ? 'cheapest' : ''].join(' ');
          const has = d.fare != null;
          const label = has
            ? `${d.date}: ${d.source === 'seen' ? 'lowest fare seen' : 'estimated lowest fare'} ${fmt(d.fare, data.currency)}` +
              (d.low != null ? `, likely ${fmt(d.low, data.currency)} to ${fmt(d.high, data.currency)}` : '')
            : `${d.date}: ${d.source === 'past' ? 'past date' : 'no estimate'}`;
          cells.push(`<button class="${cls}" data-date="${d.date}" ${d.source === 'past' ? 'disabled' : ''}
              aria-pressed="${this.selected === d.date}" title="${label}" aria-label="${label}">
              <span class="n">${n}</span>
              <span class="f">${has ? sym(data.currency) + short(d.fare) : '—'}</span>
              <span class="s">${d.source === 'seen' ? '● seen' : has ? 'est.' : ''}</span></button>`);
        }
        body = `<div class="grid">${DOW.map((w) => `<span class="dow">${w}</span>`).join('')}${cells.join('')}</div>
          <footer><span>● seen = fare an agent saw in the last 3 days</span><span>est. = ${data.model} forecast</span>
          <span>Indicative only. Reprice before booking.</span></footer>`;
      }
      this.root.innerHTML = `<style>${css}</style>
        <header><button class="nav" data-shift="-1" aria-label="Previous month">‹</button>
        <strong>${title}</strong><button class="nav" data-shift="1" aria-label="Next month">›</button></header>${body}`;

      this.root.querySelectorAll('[data-shift]').forEach((b) =>
        b.addEventListener('click', () => this.shift(Number(b.dataset.shift))));
      this.root.querySelectorAll('.day[data-date]').forEach((b) =>
        b.addEventListener('click', () => {
          const day = data.days.find((x) => x.date === b.dataset.date);
          this.selected = day.date;
          this.paint(data);
          this.dispatchEvent(new CustomEvent('fare-select', { detail: { ...day, currency: data.currency }, bubbles: true, composed: true }));
        }));
    }
  }

  if (!customElements.get('fare-calendar')) customElements.define('fare-calendar', FareCalendar);
})();
