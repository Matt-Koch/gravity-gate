// Shared helpers for admin, display and spectator pages.
const Derby = (() => {
  const listeners = [];
  let ws, state = null, retry = 500;

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { retry = 500; setConn(true); };
    ws.onclose = () => { setConn(false); setTimeout(connect, retry = Math.min(retry * 2, 8000)); };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'state') state = msg.state;
      listeners.forEach((fn) => fn(msg, state));
    };
  }
  setInterval(() => { if (ws && ws.readyState === 1) ws.send('ping'); }, 20000);

  function setConn(ok) {
    document.body.classList.toggle('offline', !ok);
  }

  function pin() { return localStorage.getItem('derbyPin') || ''; }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || (opts.body ? 'POST' : 'GET'),
      headers: { 'Content-Type': 'application/json', 'X-Admin-Pin': pin() },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (res.status === 401) {
      const p = prompt('Admin PIN');
      if (p !== null) { localStorage.setItem('derbyPin', p); return api(path, opts); }
    }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* not json */ }
      throw new Error(detail);
    }
    const type = res.headers.get('content-type') || '';
    return type.includes('json') ? res.json() : res.text();
  }

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (t) => (t == null ? '—' : Number(t).toFixed(3));
  const ord = (n) => { if (n == null) return ''; const s = ['th', 'st', 'nd', 'rd'], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); };
  function heatTitle(h) {
    if (!h) return '';
    const parts = [];
    if (h.bracket) parts.push(h.bracket === 'F' ? 'Final' : `Bracket ${h.bracket}`);
    parts.push(`Round ${h.round === 99 ? 'Make-up' : h.round}`, `Race ${h.race}`);
    return parts.join(' · ');
  }

  // Sounds are synthesized so no audio files are needed on the Pi.
  let audio;
  function beep(kind) {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const seq = { staged: [[660, 0.12]], finish: [[880, 0.12], [1320, 0.18]],
        photo: [[1200, 0.08], [0, 0.05], [1200, 0.08], [0, 0.05], [1600, 0.25]],
        award: [[523, 0.15], [659, 0.15], [784, 0.15], [1046, 0.35]] }[kind] || [[700, 0.1]];
      let t = audio.currentTime;
      for (const [f, d] of seq) {
        if (f) {
          const o = audio.createOscillator(), g = audio.createGain();
          o.type = 'triangle'; o.frequency.value = f;
          g.gain.setValueAtTime(0.25, t); g.gain.exponentialRampToValueAtTime(0.001, t + d);
          o.connect(g).connect(audio.destination); o.start(t); o.stop(t + d);
        }
        t += d;
      }
    } catch (e) { /* audio blocked until a user gesture */ }
  }

  return {
    connect, api, esc, fmt, ord, heatTitle, beep,
    on: (fn) => listeners.push(fn),
    get state() { return state; },
  };
})();
