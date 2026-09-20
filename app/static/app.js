'use strict';

const $ = (id) => document.getElementById(id);
const form = $('form'), urlInput = $('url'), fmtSelect = $('format');
const submitBtn = $('submit'), errBox = $('err'), jobsBox = $('jobs');
const historyList = $('history-list'), historyLoading = $('history-loading'), historyEmpty = $('history-empty');

const POLL_MS = 1000;

let cfg = { max_tracks: 5000, ttl_hours: 3, default_format: 'mp3-320', formats: [
  { key: 'mp3-320', label: 'MP3 · 320 kbps', kind: 'audio' },
  { key: 'mp3-192', label: 'MP3 · 192 kbps', kind: 'audio' },
  { key: 'mp3-128', label: 'MP3 · 128 kbps', kind: 'audio' },
] };
let selectedKind = 'audio';
let jobs = [];              // full job dicts from the server, newest first
let refreshing = false;
let timer = null;

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

function formatLabel(key) {
  return cfg.formats.find((f) => f.key === key)?.label || key;
}

function fillFormats() {
  const previous = fmtSelect.value;
  const choices = cfg.formats.filter((f) => f.kind === selectedKind);
  fmtSelect.replaceChildren();
  for (const f of choices) {
    const o = document.createElement('option');
    o.value = f.key;
    o.textContent = f.label;
    o.selected = f.key === previous || (!previous && f.key === cfg.default_format);
    fmtSelect.appendChild(o);
  }
}

/* -------------------------------------------------------------- rendering */

const LABELS = {
  queued: 'Warteschlange', resolving: 'Wird gelesen', downloading: 'Lädt',
  packaging: 'Packt ZIP', done: 'Fertig', error: 'Fehler', cancelled: 'Abgebrochen',
};
const ACTIVE = new Set(['queued', 'resolving', 'downloading', 'packaging']);

function render() {
  const existing = new Map([...jobsBox.children].map((el) => [el.dataset.id, el]));
  const ids = jobs.map((j) => j.id);
  for (const j of jobs) {
    const old = existing.get(j.id);
    const signature = JSON.stringify(j);
    if (old?.dataset.signature === signature) continue;
    const next = card(j);
    next.dataset.id = j.id;
    next.dataset.signature = signature;
    if (old) {
      for (const cls of ['log', 'fails']) {
        const oldD = old.querySelector(`details.${cls}`);
        const nextD = next.querySelector(`details.${cls}`);
        if (oldD?.open && nextD) nextD.open = true;
      }
      const oldPre = old.querySelector('details.log pre');
      const pinnedToBottom = !oldPre || oldPre.scrollHeight - oldPre.scrollTop - oldPre.clientHeight < 20;
      const focused = old.contains(document.activeElement);
      const actionIndex = [...old.querySelectorAll('a, button, summary')].indexOf(document.activeElement);
      old.replaceWith(next);
      if (focused) next.querySelectorAll('a, button, summary')[actionIndex]?.focus({ preventScroll: true });
      if (pinnedToBottom) {
        const newPre = next.querySelector('details.log pre');
        if (newPre) newPre.scrollTop = newPre.scrollHeight;
      }
    } else {
      const following = ids.slice(ids.indexOf(j.id) + 1).map((id) => existing.get(id)).find(Boolean);
      jobsBox.insertBefore(next, following || null);
    }
  }
  for (const [id, el] of existing) if (!ids.includes(id)) el.remove();
  $('empty').hidden = jobsBox.children.length > 0;
  $('job-count').textContent = jobsBox.children.length ? `${jobsBox.children.length} gesamt` : '';
}

function card(j) {
  const el = document.createElement('div');
  el.className = `job ${j.status}`;
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
  const unit = j.format.startsWith('video-') ? 'Videos' : 'Titel';
  meta.textContent = j.total_tracks
    ? `${j.completed_tracks} / ${j.total_tracks} ${unit} · ${formatLabel(j.format)}`
    : formatLabel(j.format);
  left.append(title, meta);

  const badge = document.createElement('span');
  badge.className = `badge ${j.source === 'spotify' ? 'sp' : 'yt'}`;
  badge.textContent = j.source === 'spotify'
    ? 'Spotify'
    : (j.format.startsWith('video-') ? 'YouTube · Video' : 'YouTube');

  head.append(left, badge);

  const bar = document.createElement('div');
  bar.className = `bar${indet ? ' indet' : ''}`;
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-label', 'Download-Fortschritt');
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', '100');
  if (!indet) bar.setAttribute('aria-valuenow', String(pct));
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
    ? bytes(j.download_size ?? j.zip_size)
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
    const extension = (j.download_name || j.zip_name || 'playlist.zip').split('.').pop().toUpperCase();
    a.download = j.download_name || j.zip_name || '';
    a.textContent = `${extension} herunterladen (${bytes(j.download_size ?? j.zip_size)})`;
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

  if (j.log_tail && j.log_tail.length) {
    const d = document.createElement('details');
    d.className = 'log';
    const s = document.createElement('summary');
    s.textContent = 'Details';
    const pre = document.createElement('pre');
    pre.className = 'log-lines';
    pre.textContent = j.log_tail.join('\n');
    d.append(s, pre);
    el.appendChild(d);
  }

  if (j.failed_count > 0) {
    const d = document.createElement('details');
    d.className = 'fails';
    const s = document.createElement('summary');
    s.textContent = `${j.failed_count} Titel übersprungen`;
    const ul = document.createElement('ul');
    for (const f of j.failed) {
      const li = document.createElement('li');
      const span = document.createElement('span');
      span.textContent = f.title;
      li.appendChild(span);
      if (j.status === 'done' && f.retryable) {
        li.appendChild(retryButton(j.id, f.index, f.retrying));
      }
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
  b.addEventListener('click', async () => {
    b.disabled = true;
    try { await fn(); } finally { b.disabled = false; }
  });
  return b;
}

function retryButton(jobId, index, retrying) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'ghost retry';
  b.textContent = retrying ? 'Lädt…' : 'Erneut versuchen';
  b.disabled = retrying;
  b.addEventListener('click', async () => {
    b.disabled = true;
    b.textContent = 'Lädt…';
    try {
      const r = await fetch(`/api/jobs/${jobId}/retry/${index}`, { method: 'POST' });
      if (r.status === 401) { location.reload(); return; }
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        showError(d.detail || 'Erneuter Versuch fehlgeschlagen.');
        // The server never got as far as marking this retrying (the request
        // was rejected before that), so nothing about the job actually
        // changed — render()'s diff would then skip this card entirely and
        // leave the button stuck showing "Lädt…" forever. Reset it directly.
        b.disabled = false;
        b.textContent = 'Erneut versuchen';
      }
    } catch {
      showError('Server nicht erreichbar.');
      b.disabled = false;
      b.textContent = 'Erneut versuchen';
    }
    refreshJobs();
  });
  return b;
}

/* ----------------------------------------------------------------- server */

async function post(path) {
  try {
    const r = await fetch(path, { method: 'POST' });
    if (r.status === 401) { location.reload(); return; }
    if (!r.ok) throw new Error();
  } catch { showError('Aktion fehlgeschlagen. Bitte erneut versuchen.'); }
  refreshJobs();
}

async function remove(id) {
  try {
    const r = await fetch(`/api/jobs/${id}`, { method: 'DELETE' });
    if (r.status === 401) { location.reload(); return; }
    if (!r.ok && r.status !== 404) throw new Error();
  } catch { showError('Download konnte nicht entfernt werden.'); return; }
  jobs = jobs.filter((j) => j.id !== id);
  render();
  refreshJobs();
}

async function refreshJobs() {
  if (refreshing) return;
  clearTimeout(timer);
  refreshing = true;
  try {
    const r = await fetch('/api/jobs', { cache: 'no-store' });
    if (r.status === 401) { location.reload(); return; }
    if (r.ok) {
      const data = await r.json();
      const prevStatus = new Map(jobs.map((j) => [j.id, j.status]));
      jobs = data.jobs || [];
      for (const j of jobs) {
        if (prevStatus.has(j.id) && prevStatus.get(j.id) !== j.status) {
          $('announcement').textContent = `${j.playlist_title || 'Download'}: ${LABELS[j.status] || j.status}`;
        }
      }
      render();
    }
  } catch { /* transient */ }
  refreshing = false;
  schedule();
}

function schedule() {
  clearTimeout(timer);
  const busy = jobs.some((j) => ACTIVE.has(j.status) || (j.failed || []).some((f) => f.retrying));
  timer = setTimeout(refreshJobs, busy ? POLL_MS : 15000);
}

/* ------------------------------------------------------------------ history */

function historyItem(h) {
  const el = document.createElement('div');
  el.className = `job history-item ${h.status}`;

  const head = document.createElement('div');
  head.className = 'job-head';
  const left = document.createElement('div');
  left.style.minWidth = '0';
  const title = document.createElement('a');
  title.className = 'job-title';
  title.href = h.url;
  title.target = '_blank';
  title.rel = 'noopener noreferrer';
  title.textContent = h.playlist_title || h.url;
  const meta = document.createElement('div');
  meta.className = 'job-meta';
  const when = new Date((h.finished_at || h.created_at) * 1000);
  const unit = h.format.startsWith('video-') ? 'Videos' : 'Titel';
  const parts = [when.toLocaleString('de-AT', { dateStyle: 'medium', timeStyle: 'short' })];
  if (h.total_tracks) parts.push(`${h.total_tracks - h.failed_count} / ${h.total_tracks} ${unit}`);
  parts.push(formatLabel(h.format));
  meta.textContent = parts.join(' · ');
  left.append(title, meta);
  const badge = document.createElement('span');
  badge.className = `badge ${h.source === 'spotify' ? 'sp' : 'yt'}`;
  badge.textContent = h.source === 'spotify' ? 'Spotify' : 'YouTube';
  head.append(left, badge);
  el.appendChild(head);

  const status = document.createElement('div');
  status.className = 'status';
  const now = document.createElement('span');
  now.className = 'now';
  now.textContent = h.status === 'error' ? (h.error || 'Fehler') : (LABELS[h.status] || h.status);
  status.appendChild(now);
  el.appendChild(status);

  const actions = document.createElement('div');
  actions.className = 'actions';
  if (h.available) {
    const a = document.createElement('a');
    a.className = 'dl';
    a.href = `/api/jobs/${h.id}/download`;
    a.download = h.download_name || '';
    a.textContent = `Herunterladen (${bytes(h.download_size)})`;
    actions.appendChild(a);
  } else {
    const span = document.createElement('span');
    span.className = 'job-meta';
    span.textContent = 'Datei nicht mehr verfügbar';
    actions.appendChild(span);
  }
  el.appendChild(actions);
  return el;
}

async function loadHistory() {
  historyList.replaceChildren();
  historyEmpty.hidden = true;
  historyLoading.hidden = false;
  try {
    const r = await fetch('/api/history', { cache: 'no-store' });
    if (r.status === 401) { location.reload(); return; }
    const data = await r.json();
    const items = data.items || [];
    historyLoading.hidden = true;
    historyEmpty.hidden = items.length > 0;
    for (const h of items) historyList.appendChild(historyItem(h));
  } catch {
    historyLoading.hidden = true;
    showError('Verlauf konnte nicht geladen werden.');
  }
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
    jobs = [data, ...jobs.filter((j) => j.id !== data.id)];
    urlInput.value = '';
    render();
    refreshJobs();
  } catch {
    showError('Server nicht erreichbar.');
  } finally {
    submitBtn.disabled = false;
    submitBtn.innerHTML = 'Download starten <span aria-hidden="true">↓</span>';
  }
});

document.querySelectorAll('input[name="kind"]').forEach((input) => {
  input.addEventListener('change', () => {
    selectedKind = input.value;
    fillFormats();
  });
});

(async function init() {
  // A link shared into the installed app (or a hand-built deep link) arrives
  // as ?url=...&format=...&kind=... — prefill and start it directly instead
  // of leaving the visitor to paste the link in again.
  const shared = new URLSearchParams(location.search);
  const sharedUrl = shared.get('url');
  if (sharedUrl) {
    urlInput.value = sharedUrl;
    const kind = shared.get('kind');
    if (kind === 'audio' || kind === 'video') {
      selectedKind = kind;
      const radio = document.querySelector(`input[name="kind"][value="${kind}"]`);
      if (radio) radio.checked = true;
    }
  }
  if (location.search) history.replaceState(null, '', location.pathname);

  try {
    const r = await fetch('/api/config');
    if (r.status === 401) { location.reload(); return; }
    cfg = await r.json();
    fillFormats();
    if (sharedUrl) {
      const fmt = shared.get('format');
      if (fmt && cfg.formats.some((f) => f.key === fmt)) fmtSelect.value = fmt;
    }
    $('footer').textContent =
      `Max. ${cfg.max_tracks} Titel pro Playlist · Downloads werden nach ${cfg.ttl_hours} h gelöscht`;
  } catch { /* keep defaults */ }
  refreshJobs();
  loadHistory();
  if (sharedUrl) form.requestSubmit();
})();
