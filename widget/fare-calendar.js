/**
 * <fare-calendar>: drop-in date picker that shows the lowest fare under every date.
 *
 *   <script src="https://YOUR-HOST/widget/fare-calendar.js"></script>
 *   <fare-calendar api="https://YOUR-HOST" origin="DEL" destination="BOM"></fare-calendar>
 *
 * Fires `fare-select` with { date, fare, low, high, source, seen_at, cheapest, currency }.
 * Attributes: api, origin, destination, month (YYYY-MM, defaults to this month),
 *             api-key (sent as X-Api-Key when the service is protected; prefer proxying).
 * Theming: set --fc-* custom properties on the element. Fonts inherit from the page.
 */
(() => {
  const css = `
  :host {
    --fc-surface: #fbfcfc; --fc-ink: #15304a; --fc-muted: #5b6b7a; --fc-line: #c9d3da;
    --fc-accent: #b0186e; --fc-low: #1d7a5f; --fc-mid: #8a6a14; --fc-high: #a33a2b;
    --fc-bar: #c9d3da; --fc-focus: #b0186e;
    display: block; color: var(--fc-ink); background: var(--fc-surface);
    border: 1px solid var(--fc-line); border-radius: 6px; padding: 14px;
    font: inherit; font-size: 14px; line-height: 1.3; max-width: 480px;
    font-variant-numeric: tabular-nums;
  }
  @media (prefers-color-scheme: dark) {
    :host {
      --fc-surface: #13212d; --fc-ink: #dce6ee; --fc-muted: #93a4b3; --fc-line: #2b3d4c;
      --fc-accent: #e0559f; --fc-low: #5cc5a2; --fc-mid: #d8b04a; --fc-high: #ef7b66;
      --fc-bar: #2f4354; --fc-focus: #e0559f;
    }
  }
  * { box-sizing: border-box; }
  .head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .title { font-size: 16px; font-weight: 700; letter-spacing: -0.01em; }
  .nav { border: 1px solid var(--fc-line); background: transparent; color: inherit; border-radius: 6px;
         width: 34px; height: 34px; font: inherit; font-size: 18px; cursor: pointer; }
  .nav:hover { border-color: var(--fc-ink); }
  button:focus-visible, .bar:focus-visible { outline: 2px solid var(--fc-focus); outline-offset: 2px; }

  /* The fare profile: one bar per day, troughs = cheap days. */
  .profile { margin: 12px 0 6px; height: 46px; display: grid; gap: 2px; align-items: end; }
  .bar { height: var(--h); min-height: 3px; background: var(--fc-bar); border-radius: 2px 2px 0 0;
         cursor: pointer; border: 0; padding: 0; }
  .bar.est { background: repeating-linear-gradient(0deg, var(--fc-bar) 0 3px, transparent 3px 5px);
             box-shadow: inset 0 0 0 1px var(--fc-bar); }
  .bar.cheap { background: var(--fc-accent); box-shadow: none; }
  .bar.none { height: 3px; opacity: .4; cursor: default; }
  .bar.hot, .bar.sel { outline: 2px solid var(--fc-ink); outline-offset: 1px; }
  .profile-note { display: flex; justify-content: space-between; color: var(--fc-muted); font-size: 12px;
                  margin-bottom: 10px; min-height: 16px; }
  .profile-note b { color: var(--fc-accent); font-weight: 700; }

  .grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 3px; }
  .dow { text-align: center; color: var(--fc-muted); font-size: 12px; padding-bottom: 2px; }
  .day { position: relative; border: 1px solid var(--fc-line); background: transparent; color: inherit;
         border-radius: 4px; min-height: 54px; padding: 5px 2px 4px; font: inherit; cursor: pointer;
         display: flex; flex-direction: column; align-items: center; gap: 3px; }
  .day:hover, .day.hot { border-color: var(--fc-ink); }
  .day:disabled { opacity: .35; cursor: default; border-style: dashed; }
  .n { font-size: 12px; color: var(--fc-muted); }
  .f { font-size: 14px; font-weight: 700; }
  .low .f { color: var(--fc-low); } .mid .f { color: var(--fc-mid); } .high .f { color: var(--fc-high); }
  .est .f { font-weight: 500; text-decoration: underline dotted; text-underline-offset: 3px; }
  .f { white-space: nowrap; }
  .seen::before { content: ""; position: absolute; top: 6px; left: 6px; width: 6px; height: 6px;
                  border-radius: 50%; background: var(--fc-ink); }
  .cheap { border-color: var(--fc-accent); }
  .cheap::after { content: ""; position: absolute; top: 0; right: 0; border: 7px solid transparent;
                  border-top-color: var(--fc-accent); border-right-color: var(--fc-accent); border-radius: 0 3px 0 0; }
  .day[aria-pressed="true"] { outline: 2px solid var(--fc-accent); outline-offset: 1px; }
  .none .f { color: var(--fc-muted); font-weight: 400; }

  .foot { margin-top: 10px; color: var(--fc-muted); font-size: 12px; display: grid; gap: 3px; }
  .foot .seen-key::before { content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%;
                            background: var(--fc-ink); margin-right: 5px; vertical-align: 1px; }
  .foot .est-key { text-decoration: underline dotted; text-underline-offset: 3px; }

  .msg { padding: 20px 8px; text-align: center; color: var(--fc-muted); display: grid; gap: 10px; justify-items: center; }
  .retry { border: 1px solid var(--fc-ink); background: transparent; color: var(--fc-ink); border-radius: 6px;
           padding: 7px 14px; font: inherit; cursor: pointer; }
  .skel .day { cursor: default; }
  .skel .f { width: 60%; height: 12px; border-radius: 3px; background: var(--fc-line); }
  @media (prefers-reduced-motion: no-preference) {
    .skel .f { animation: pulse 1.2s ease-in-out infinite; }
    @keyframes pulse { 50% { opacity: .45; } }
  }
  @media (max-width: 360px) { .day { min-height: 48px; } .f { font-size: 12px; } }`;

  const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const pad = (n) => String(n).padStart(2, '0');

  class FareCalendar extends HTMLElement {
    static get observedAttributes() { return ['origin', 'destination', 'month', 'api', 'api-key']; }

    constructor() {
      super();
      this.root = this.attachShadow({ mode: 'open' });
      this.cache = new Map();
      this.selected = null;
      this.focusDate = null;
    }

    connectedCallback() { this.load(); }
    attributeChangedCallback(name, oldV, newV) {
      if (oldV === newV || !this.isConnected) return;
      if (name === 'origin' || name === 'destination') this.selected = null;
      this.load();
    }

    get month() {
      const m = this.getAttribute('month');
      if (m && /^\d{4}-\d{2}$/.test(m)) return m;
      const d = new Date();
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
    }

    shift(delta) {
      const [y, m] = this.month.split('-').map(Number);
      const d = new Date(y, m - 1 + delta, 1);
      this.setAttribute('month', `${d.getFullYear()}-${pad(d.getMonth() + 1)}`);
    }

    url(month) {
      const api = (this.getAttribute('api') || '').replace(/\/$/, '');
      const q = new URLSearchParams({ origin: this.getAttribute('origin'), destination: this.getAttribute('destination'), month });
      return `${api}/api/calendar?${q}`;
    }

    async fetchMonth(month) {
      const url = this.url(month);
      if (!this.cache.has(url)) {
        const key = this.getAttribute('api-key');
        const p = fetch(url, key ? { headers: { 'X-Api-Key': key } } : {}).then(async (res) => {
          const ct = res.headers.get('content-type') || '';
          const body = ct.includes('json') ? await res.json() : null;
          if (!res.ok) {
            const why = body?.detail || (res.status === 401 ? 'Sign in to see fares.' : `The fare service answered ${res.status}.`);
            throw new Error(why);
          }
          return body;
        });
        this.cache.set(url, p);
        p.catch(() => this.cache.delete(url));
      }
      return this.cache.get(url);
    }

    async load() {
      const o = this.getAttribute('origin'), d = this.getAttribute('destination');
      if (!o || !d) return this.paintMessage('Choose where from and where to, and the cheapest days show up here.');
      const month = this.month;
      this.paintSkeleton();
      const slow = setTimeout(() => {
        const note = this.root.querySelector('.profile-note');
        if (note) note.textContent = 'Waking the fare service. The first load can take up to a minute.';
      }, 4000);
      try {
        const data = await this.fetchMonth(month);
        if (month !== this.month) return;            // the user moved on meanwhile
        this.data = data;
        this.paint();
        const [y, m] = month.split('-').map(Number); // warm the next month
        const next = new Date(y, m, 1);
        this.fetchMonth(`${next.getFullYear()}-${pad(next.getMonth() + 1)}`).catch(() => {});
      } catch (e) {
        if (month === this.month) this.paintMessage(`Couldn't load fares for ${o} to ${d}. ${e.message}`, true);
      } finally {
        clearTimeout(slow);
      }
    }

    frame(inner) {
      const [y, m] = this.month.split('-').map(Number);
      const title = new Date(y, m - 1, 1).toLocaleString(undefined, { month: 'long', year: 'numeric' });
      this.root.innerHTML = `<style>${css}</style>
        <div class="head">
          <button class="nav" data-shift="-1" aria-label="Previous month">‹</button>
          <span class="title" aria-live="polite">${title}</span>
          <button class="nav" data-shift="1" aria-label="Next month">›</button>
        </div>${inner}`;
      this.root.querySelectorAll('[data-shift]').forEach((b) => b.addEventListener('click', () => this.shift(Number(b.dataset.shift))));
    }

    paintMessage(text, retry = false) {
      this.frame(`<div class="msg"><span>${text}</span>${retry ? '<button class="retry">Try again</button>' : ''}</div>`);
      this.root.querySelector('.retry')?.addEventListener('click', () => this.load());
    }

    paintSkeleton() {
      const [y, m] = this.month.split('-').map(Number);
      const n = new Date(y, m, 0).getDate();
      const lead = (new Date(y, m - 1, 1).getDay() + 6) % 7;
      const cells = Array.from({ length: lead }, () => '<span></span>').join('') +
        Array.from({ length: n }, (_, i) => `<div class="day"><span class="n">${i + 1}</span><span class="f"></span></div>`).join('');
      this.frame(`<div class="profile"></div><div class="profile-note">Loading fares…</div>
        <div class="grid skel" aria-busy="true">${DOW.map((w) => `<span class="dow">${w}</span>`).join('')}${cells}</div>`);
    }

    paint() {
      const data = this.data;
      const [y, m] = this.month.split('-').map(Number);
      const money = (n) => new Intl.NumberFormat(undefined, { style: 'currency', currency: data.currency, maximumFractionDigits: 0 }).format(n);
      const sym = new Intl.NumberFormat(undefined, { style: 'currency', currency: data.currency, currencyDisplay: 'narrowSymbol' })
        .formatToParts(0).find((p) => p.type === 'currency')?.value || '';
      const short = (n) => sym + (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : n);

      const priced = data.days.filter((d) => d.fare != null);
      const max = Math.max(...priced.map((d) => d.fare), 1);
      const min = Math.min(...priced.map((d) => d.fare), max);
      const cheapest = priced.filter((d) => d.cheapest);
      const bars = data.days.map((d) => {
        if (d.fare == null) return `<span class="bar none" aria-hidden="true"></span>`;
        const h = 18 + 82 * ((d.fare - min) / Math.max(max - min, 1));
        const cls = ['bar', d.source === 'estimate' ? 'est' : '', d.cheapest ? 'cheap' : '', this.selected === d.date ? 'sel' : ''].join(' ');
        return `<button class="${cls}" style="--h:${h.toFixed(0)}%" data-date="${d.date}" tabindex="-1"
                 aria-label="${d.date} ${money(d.fare)}"></button>`;
      }).join('');
      const note = cheapest.length
        ? `<span>Cheapest: <b>${cheapest.map((d) => Number(d.date.slice(8))).slice(0, 4).join(', ')}${cheapest.length > 4 ? '…' : ''}</b> at ${money(cheapest[0].fare)}</span><span>${priced.length} of ${data.days.length} days priced</span>`
        : `<span>No fares for this month yet.</span>`;

      const lead = (new Date(y, m - 1, 1).getDay() + 6) % 7;
      const firstOpen = data.days.find((d) => d.source !== 'past')?.date;
      if (!this.focusDate || !data.days.some((d) => d.date === this.focusDate && d.source !== 'past')) {
        this.focusDate = this.selected?.startsWith(this.month) ? this.selected : firstOpen;
      }
      const cells = Array.from({ length: lead }, () => '<span></span>');
      for (const d of data.days) {
        const n = Number(d.date.slice(8));
        const has = d.fare != null;
        const past = d.source === 'past';
        const cls = ['day', d.tier || '', has ? (d.source === 'seen' ? 'seen' : 'est') : 'none', d.cheapest ? 'cheap' : ''].join(' ');
        const label = past ? `${d.date}, past date`
          : !has ? `${d.date}, no fare yet`
          : `${d.date}, ${d.source === 'seen' ? 'lowest fare seen' : 'estimated lowest fare'} ${money(d.fare)}` +
            (d.low != null ? `, likely ${money(d.low)} to ${money(d.high)}` : '') + (d.cheapest ? ', cheapest this month' : '');
        cells.push(`<button class="${cls}" data-date="${d.date}" ${past ? 'disabled' : ''}
            tabindex="${d.date === this.focusDate ? 0 : -1}" aria-pressed="${this.selected === d.date}" aria-label="${label}" title="${label}">
            <span class="n">${n}</span><span class="f">${has ? short(d.fare) : '–'}</span></button>`);
      }

      this.frame(`
        <div class="profile" style="grid-template-columns: repeat(${data.days.length}, 1fr)" aria-hidden="true">${bars}</div>
        <div class="profile-note">${note}</div>
        <div class="grid" role="group" aria-label="Days of ${this.month}">${DOW.map((w) => `<span class="dow">${w}</span>`).join('')}${cells.join('')}</div>
        <div class="foot">
          <span><span class="seen-key">Seen</span>: a fare agents got in the last 3 days. <span class="est-key">Dotted</span>: estimated by ${data.model.split('/').pop()}.</span>
          <span>Indicative only. Reprice before booking.</span>
        </div>`);
      this.wire();
    }

    wire() {
      const days = [...this.root.querySelectorAll('.day[data-date]')];
      const bars = [...this.root.querySelectorAll('.bar[data-date]')];
      const hot = (date, on) => this.root.querySelectorAll(`[data-date="${date}"]`).forEach((el) => el.classList.toggle('hot', on));
      for (const el of [...days, ...bars]) {
        el.addEventListener('mouseenter', () => hot(el.dataset.date, true));
        el.addEventListener('mouseleave', () => hot(el.dataset.date, false));
        el.addEventListener('click', () => !el.disabled && this.select(el.dataset.date));
      }
      this.root.querySelector('.grid')?.addEventListener('keydown', (e) => {
        const steps = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
        if (!(e.key in steps)) return;
        e.preventDefault();
        const i = days.findIndex((b) => b === this.root.activeElement);
        const j = i + steps[e.key];
        if (j < 0 || j >= days.length) return this.shift(j < 0 ? -1 : 1);
        if (days[j].disabled) return;
        days[j].focus();
      });
      // Roving tab stop: whichever day has focus is the only one Tab reaches.
      this.root.querySelector('.grid')?.addEventListener('focusin', (e) => {
        const el = e.target.closest?.('.day[data-date]');
        if (!el) return;
        days.forEach((b) => { b.tabIndex = b === el ? 0 : -1; });
        this.focusDate = el.dataset.date;
      });
    }

    select(date) {
      const day = this.data.days.find((x) => x.date === date);
      if (!day) return;
      this.selected = this.focusDate = date;
      this.paint();
      this.root.querySelector(`.day[data-date="${date}"]`)?.focus();
      this.dispatchEvent(new CustomEvent('fare-select', {
        detail: { ...day, currency: this.data.currency, model: this.data.model, as_of: this.data.as_of }, bubbles: true, composed: true,
      }));
    }
  }

  if (!customElements.get('fare-calendar')) customElements.define('fare-calendar', FareCalendar);
})();
