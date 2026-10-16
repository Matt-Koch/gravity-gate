// Bracket "line view": rounds left -> right, Bracket A on top, Bracket B (and C) below,
// Final + Champion on the right. Each racer is traced with a line from the matchup they
// survived to the next one they race in: gold when they advance in the same bracket,
// red dashed when a loss drops them down a bracket. Multi-lane elimination redraws heats
// every round (to avoid rematches), so lines follow racers rather than a fixed tree.
const BracketLines = (() => {
  const BOX_W = 210, ROW_H = 24, HEAD_H = 20, PAD = 6, COL_GAP = 80, BOX_GAP = 14, SECTION_GAP = 46, TOP = 34;
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const sectionOf = (b) => (b === 'F' ? 'A' : b);           // the Final sits in the top row
  const label = (b) => (b === 'F' ? 'Final' : `Bracket ${b}`);

  function layout(data) {
    const rounds = data.rounds || [];
    const pos = {};          // racer_id -> {x, y} of their row in the most recent matchup
    const boxes = [], lines = [];
    const sections = [...new Set(rounds.flatMap((r) => r.matchups.map((m) => sectionOf(m.bracket))))].sort();
    // Rows per section = the most matchups that section ever needs; keeps sections aligned across columns.
    const sectionHeight = {};
    for (const s of sections) {
      sectionHeight[s] = Math.max(...rounds.map((r) => r.matchups.filter((m) => sectionOf(m.bracket) === s)
        .reduce((h, m) => h + HEAD_H + m.racers.length * ROW_H + PAD * 2 + BOX_GAP, 0)), 0);
    }
    const sectionTop = {};
    let y0 = TOP;
    for (const s of sections) { sectionTop[s] = y0; y0 += sectionHeight[s] + SECTION_GAP; }
    const height = y0 - SECTION_GAP + 10;

    rounds.forEach((round, ci) => {
      const x = ci * (BOX_W + COL_GAP);
      for (const s of sections) {
        // Barycenter ordering: put each matchup near where its racers came from -> fewer crossings.
        const ms = round.matchups.filter((m) => sectionOf(m.bracket) === s).map((m) => {
          const ys = m.racers.map((r) => pos[r.racer_id]?.y).filter((v) => v != null);
          return { m, key: ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : 1e9 };
        }).sort((a, b) => a.key - b.key);
        let y = sectionTop[s];
        const total = ms.reduce((h, { m }) => h + HEAD_H + m.racers.length * ROW_H + PAD * 2 + BOX_GAP, 0) - BOX_GAP;
        y += Math.max(0, (sectionHeight[s] - BOX_GAP - total) / 2);   // centre short columns
        for (const { m } of ms) {
          const h = HEAD_H + m.racers.length * ROW_H + PAD * 2;
          // Racers who lost here and are out don't continue; everyone else draws a line in.
          m.racers.forEach((r, i) => {
            const ry = y + HEAD_H + PAD + i * ROW_H + ROW_H / 2;
            const from = pos[r.racer_id];
            if (from) lines.push({ x1: from.x, y1: from.y, x2: x, y2: ry, drop: from.lost, racer: r.racer_id });
            pos[r.racer_id] = { x: x + BOX_W, y: ry, lost: r.lost };
          });
          boxes.push({ x, y, h, m, round: round.round });
          y += h + BOX_GAP;
        }
      }
    });
    let width = rounds.length * (BOX_W + COL_GAP) - COL_GAP;
    let champ = null;
    if (data.champion) {
      const from = pos[data.champion.racer_id];
      const x = width + COL_GAP;
      const y = from ? from.y - 26 : TOP;
      champ = { x, y, racer: data.champion };
      if (from) lines.push({ x1: from.x, y1: from.y, x2: x, y2: y + 26, drop: false, champion: true });
      width = x + BOX_W;
    }
    return { boxes, lines, champ, width: width + 10, height: Math.max(height, (champ?.y || 0) + 70), rounds };
  }

  function render(el, data, opts = {}) {
    if (!data || data.kind !== 'elimination' || !(data.rounds || []).length) {
      el.innerHTML = '<p class="muted">No elimination bracket yet.</p>';
      return;
    }
    const L = layout(data);
    const follow = opts.follow || new Set();
    const path = (l) => {
      const mx = (l.x1 + l.x2) / 2;
      return `M${l.x1},${l.y1} C${mx},${l.y1} ${mx},${l.y2} ${l.x2},${l.y2}`;
    };
    const heads = L.rounds.map((r, i) => `<div class="bl-colhead" style="left:${i * (BOX_W + COL_GAP)}px;width:${BOX_W}px">Round ${r.round}</div>`).join('')
      + (L.champ ? `<div class="bl-colhead" style="left:${L.champ.x}px;width:${BOX_W}px">Champion</div>` : '');
    const svg = `<svg class="bl-svg" width="${L.width}" height="${L.height}" aria-hidden="true">
      ${L.lines.map((l) => `<path d="${path(l)}" class="bl-line ${l.drop ? 'drop' : ''} ${l.champion ? 'champ' : ''} ${follow.has(l.racer) ? 'mine' : ''}"/>`).join('')}</svg>`;
    const boxes = L.boxes.map((b) => `<div class="bl-box ${b.m.done ? 'done' : 'pending'} br-${b.m.bracket}" style="left:${b.x}px;top:${b.y}px;width:${BOX_W}px;height:${b.h}px">
        <div class="bl-head">${label(b.m.bracket)} · Race ${b.m.race}${b.m.done ? '' : ' · <b>up next</b>'}</div>
        ${b.m.racers.map((r) => `<div class="bl-racer ${r.lost ? 'lost' : (b.m.done ? 'won' : '')} ${follow.has(r.racer_id) ? 'mine' : ''}">
          <span class="bl-car">#${esc(r.car_number)}</span><span class="bl-name">${esc(r.name)}</span>
          <span class="bl-mark">${r.lost ? (r.out ? '✕' : '↓') : (b.m.done ? '→' : '')}</span></div>`).join('')}
      </div>`).join('');
    const champ = L.champ ? `<div class="bl-champ" style="left:${L.champ.x}px;top:${L.champ.y}px;width:${BOX_W}px">🏆 #${esc(L.champ.racer.car_number)} ${esc(L.champ.racer.name)}</div>` : '';
    el.innerHTML = `<div class="bl-canvas" style="width:${L.width}px;height:${L.height}px">${heads}${svg}${boxes}${champ}</div>`;
    const c = el.firstElementChild;
    if (opts.fit === 'width') {          // phone overview: whole bracket across the screen
      const s = Math.min(1, el.clientWidth / L.width);
      c.style.transform = `scale(${s})`;
      c.style.transformOrigin = 'top left';
      el.style.height = `${L.height * s}px`;
    } else if (opts.fit) {               // projector: fit both ways
      const s = Math.min(1.6, el.clientWidth / L.width, el.clientHeight / L.height);
      c.style.transform = `scale(${s})`;
      c.style.transformOrigin = 'top left';
    } else {
      el.style.height = '';
    }
    return L;
  }

  // x of the newest round column, so a scroller can open on the action.
  function latestX(data) {
    const n = (data.rounds || []).length;
    return Math.max(0, (n - 1) * (BOX_W + COL_GAP) - 24);
  }

  const css = `
  .bl-canvas { position: relative; font-size: 13px; }
  .bl-svg { position: absolute; inset: 0; overflow: visible; }
  .bl-line { fill: none; stroke: var(--gold); stroke-width: 2.5; opacity: .85; }
  .bl-line.drop { stroke: var(--red); stroke-dasharray: 6 5; opacity: .7; }
  .bl-line.champ { stroke-width: 4; }
  .bl-line.mine { stroke: #7fd3ff; stroke-width: 4; opacity: 1; }
  .bl-colhead { position: absolute; top: 0; color: var(--gold); font-weight: 700; text-transform: uppercase; letter-spacing: .08em; font-size: 12px; }
  .bl-box { position: absolute; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: ${PAD}px 8px; box-sizing: border-box; }
  .bl-box.pending { border-color: var(--blue); box-shadow: 0 0 0 2px rgba(61, 107, 255, .25); }
  .bl-box.br-B { background: #1a1424; } .bl-box.br-C { background: #221416; } .bl-box.br-F { border-color: var(--gold); }
  .bl-head { height: ${HEAD_H}px; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }
  .bl-head b { color: #9db4ff; }
  .bl-racer { height: ${ROW_H}px; display: flex; align-items: center; gap: 6px; white-space: nowrap; overflow: hidden; }
  .bl-car { color: var(--muted); width: 34px; flex: none; font-variant-numeric: tabular-nums; }
  .bl-name { flex: 1; overflow: hidden; text-overflow: ellipsis; }
  .bl-mark { width: 14px; text-align: center; color: var(--gold); }
  .bl-racer.lost .bl-name { color: var(--red); text-decoration: line-through; }
  .bl-racer.lost .bl-mark { color: var(--red); }
  .bl-racer.won .bl-name { font-weight: 600; }
  .bl-racer.mine { background: rgba(127, 211, 255, .15); border-radius: 4px; }
  .bl-champ { position: absolute; height: 52px; display: flex; align-items: center; justify-content: center; border: 2px solid var(--gold);
    border-radius: 12px; background: rgba(255, 210, 63, .12); color: var(--gold); font-weight: 800; font-size: 15px; padding: 0 8px; text-align: center; }`;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  return { render, layout, latestX };
})();
