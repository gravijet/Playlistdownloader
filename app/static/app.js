'use strict';

const $ = (id) => document.getElementById(id);
const form = $('form'), urlInput = $('url'), fmtSelect = $('format');
const submitBtn = $('submit'), errBox = $('err'), jobsBox = $('jobs');

const STORE_KEY = 'ytdlweb.jobs';
const POLL_MS = 1500;

let cfg = { max_tracks: 300, ttl_hours: 3 };
let tracked = load();          // [jobId, …], newest first
const cache = new Map();       // jobId -> last status payload
let timer = null;

/* ---------------------------------------------------------------- storage */

function load() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY)) || []; }
  catch { return []; }
}
function save() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(tracked.slice(0, 25))); }
  catch { /* private mode */ }
}

/* ------------------------------------------------------------------ utils */

function bytes(n) {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

function mins(sec) {
  if (sec == null) return '';
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h > 0 ? `${h} h ${m} min` : `${m} min`;
}

function showError(msg) {
  errBox.textContent = msg;
  errBox.hidden = false;
}

/* -------------------------------------------------------------- rendering */

const LABELS = {
  queued: 'Warteschlange', resolving: 'Wird gelesen', downloading: 'Lädt',
  packaging: 'Packt ZIP', done: 'Fertig', error: 'Fehler', cancelled: 'Abgebrochen',
};
const ACTIVE = new Set(['queued', 'resolving', 'downloading', 'packaging']);

function render() {
  jobsBox.replaceChildren();
  for (const id of tracked) {
    const j = cache.get(id);
    if (j) jobsBox.appendChild(card(j));
  }
}

function card(j) {
  const el = document.createElement('div');
  el.className = `card job ${j.status}`;
  const indet = j.status === 'resolving' || j.status === 'queued';
  const pct = Math.round(j.progress * 100);

  const head = document.createElement('div');
  head.className = 'job-head';

  const left = document.createElement('div');
  left.style.minWidth = '0';
  const title = document.createElement('div');
  title.className = 'job-title';
  title.textContent = j.playlist_title || j.url;
  const meta = document.createElement('div');
  meta.className = 'job-meta';
  meta.textContent = j.total_tracks
    ? `${j.completed_tracks} / ${j.total_tracks} Titel · ${j.format}`
    : j.format;
  left.append(title, meta);

  const badge = document.createElement('span');
  badge.className = `badge ${j.source === 'spotify' ? 'sp' : 'yt'}`;
  badge.textContent = j.source === 'spotify' ? 'Spotify' : 'YouTube';

  head.append(left, badge);

  const bar = document.createElement('div');
  bar.className = `bar${indet ? ' indet' : ''}`;
  const fill = document.createElement('i');
  fill.style.width = indet ? '' : `${pct}%`;
  bar.appendChild(fill);

  const status = document.createElement('div');
  status.className = 'status';
  const now = document.createElement('span');
  now.className = 'now';
  now.textContent = j.status === 'error'
    ? (j.error || 'Fehler')
    : (j.current_track || j.message || LABELS[j.status] || j.status);
  const right = document.createElement('span');
  right.textContent = j.status === 'done'
    ? bytes(j.zip_size)
    : (ACTIVE.has(j.status) && !indet ? `${pct} %` : LABELS[j.status] || '');
  status.append(now, right);

  el.append(head, bar, status);

  /* actions */
  const actions = document.createElement('div');
  actions.className = 'actions';

  if (j.status === 'done') {
    const a = document.createElement('a');
    a.className = 'dl';
    a.href = `/api/jobs/${j.id}/download`;
    a.textContent = `ZIP herunterladen (${bytes(j.zip_size)})`;
    actions.appendChild(a);
    if (j.expires_in != null) {
      const exp = document.createElement('span');
      exp.className = 'job-meta';
      exp.style.alignSelf = 'center';
      exp.textContent = `wird in ${mins(j.expires_in)} gelöscht`;
      actions.appendChild(exp);
    }
  }

  if (ACTIVE.has(j.status)) {
    actions.appendChild(btn('Abbrechen', () => post(`/api/jobs/${j.id}/cancel`)));
  } else {
    actions.appendChild(btn('Entfernen', () => remove(j.id)));
  }
  el.appendChild(actions);

  if (j.failed_count > 0) {
    const d = document.createElement('details');
    d.className = 'fails';
    const s = document.createElement('summary');
    s.textContent = `${j.failed_count} Titel übersprungen`;
    const ul = document.createElement('ul');
    for (const f of j.failed_tracks) {
      const li = document.createElement('li');
      li.textContent = f;
      ul.appendChild(li);
    }
    d.append(s, ul);
    el.appendChild(d);
  }
  return el;
}

function btn(text, fn) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'ghost';
  b.textContent = text;
  b.addEventListener('click', () => { b.disabled = true; fn(); });
  return b;
}

/* ----------------------------------------------------------------- server */

async function post(path) {
  try { await fetch(path, { method: 'POST' }); } catch { /* next poll shows it */ }
  poll();
}

async function remove(id) {
  try { await fetch(`/api/jobs/${id}`, { method: 'DELETE' }); } catch { /* ignore */ }
  tracked = tracked.filter((x) => x !== id);
  cache.delete(id);
  save();
  render();
}

async function poll() {
  if (!tracked.length) { schedule(); return; }
  const gone = [];
  await Promise.all(tracked.map(async (id) => {
    try {
      const r = await fetch(`/api/jobs/${id}`, { cache: 'no-store' });
      if (r.status === 401) { location.reload(); return; }
      if (r.status === 404) { gone.push(id); return; }
      if (r.ok) cache.set(id, await r.json());
    } catch { /* transient */ }
  }));
  if (gone.length) {
    tracked = tracked.filter((x) => !gone.includes(x));
    gone.forEach((id) => cache.delete(id));
    save();
  }
  render();
  schedule();
}

function schedule() {
  clearTimeout(timer);
  const busy = tracked.some((id) => ACTIVE.has(cache.get(id)?.status));
  timer = setTimeout(poll, busy ? POLL_MS : 15000);
}

/* ------------------------------------------------------------------- init */

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  errBox.hidden = true;
  const url = urlInput.value.trim();
  if (!url) return;

  submitBtn.disabled = true;
  submitBtn.textContent = 'Starte…';
  try {
    const r = await fetch('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, format: fmtSelect.value }),
    });
    if (r.status === 401) { location.reload(); return; }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { showError(data.detail || 'Start fehlgeschlagen.'); return; }
    tracked.unshift(data.id);
    cache.set(data.id, data);
    save();
    urlInput.value = '';
    render();
    poll();
  } catch {
    showError('Server nicht erreichbar.');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Herunterladen';
  }
});

(async function init() {
  try {
    const r = await fetch('/api/config');
    if (r.status === 401) { location.reload(); return; }
    cfg = await r.json();
    for (const f of cfg.formats) {
      const o = document.createElement('option');
      o.value = f.key;
      o.textContent = f.label;
      if (f.key === cfg.default_format) o.selected = true;
      fmtSelect.appendChild(o);
    }
    $('footer').textContent =
      `Max. ${cfg.max_tracks} Titel pro Playlist · fertige ZIPs werden nach ${cfg.ttl_hours} h gelöscht`;
  } catch { /* keep defaults */ }
  poll();
})();
