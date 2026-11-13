/* Cens-arr's page: the library, the queue, what has been censored, settings.

   No framework and no build step. Views are plain functions that write HTML
   into their section; every button carries a data-action that one listener
   dispatches, so there is one place to look for what a click does. */
'use strict';

/* ======================================================================
   Small helpers
   ====================================================================== */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const esc = (value) => String(value ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const icon = (name, cls = 'i') =>
  `<svg class="${cls}" aria-hidden="true" focusable="false"><use href="#i-${name}"/></svg>`;

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const pad2 = (n) => String(n ?? 0).padStart(2, '0');
const epCode = (season, episode) => `S${pad2(season)}E${pad2(episode)}`;

const stamp = (seconds) => {
  const s = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(s / 3600);
  const mm = `${pad2(Math.floor((s % 3600) / 60))}:${pad2(s % 60)}`;
  return h ? `${h}:${mm}` : mm;
};

const size = (bytes) => {
  if (!bytes) return '';
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  return `${Math.max(1, Math.round(bytes / 1e6))} MB`;
};

const when = (epoch) => (epoch
  ? new Date(epoch * 1000).toLocaleDateString(undefined,
    { day: 'numeric', month: 'short', year: 'numeric' })
  : '');

/* "Two days ago" reads faster than a date when the point is what is new. */
const ago = (iso) => {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const days = Math.floor((Date.now() - t) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 14) return `${days} days ago`;
  return new Date(t).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
};

const elapsed = (startedAt) => {
  const s = Math.max(0, Math.round(Date.now() / 1000 - startedAt));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${pad2(s % 60)}s`;
};

const debounce = (fn, ms) => {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
};

/* ======================================================================
   Talking to the server
   ====================================================================== */

class ApiError extends Error {
  constructor(message, status = 0, errors = null) {
    super(message);
    this.status = status;
    this.errors = errors;
  }
}

async function api(path, { method = 'GET', body } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    throw new ApiError('Cannot reach Cens-arr. Is the container running?');
  }
  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    // The session expired, or a login was just turned on elsewhere.
    showGate('login');
    throw new ApiError('signed out', 401);
  }
  if (!res.ok) {
    let message = res.statusText || `error ${res.status}`;
    let errors = null;
    try {
      const data = await res.json();
      if (typeof data.detail === 'string') message = data.detail;
      errors = data.errors || null;
    } catch (e) { /* not JSON */ }
    throw new ApiError(message, res.status, errors);
  }
  return res.status === 204 ? null : res.json();
}

/* ======================================================================
   Toasts and questions
   ====================================================================== */

function toast(message, { action, error = false, ms = 4200 } = {}) {
  // An open modal makes the rest of the page inert, so a toast (and its Undo)
  // has to live inside it to be seen and clicked.
  const modal = $$('dialog[open]').pop();
  const host = modal ? (modal.querySelector(':scope > .toasts')
    || modal.appendChild(Object.assign(document.createElement('div'), { className: 'toasts' })))
    : $('#toasts');
  const el = document.createElement('div');
  el.className = `toast${error ? ' error' : ''}`;
  el.setAttribute('role', error ? 'alert' : 'status');
  el.innerHTML = `<span>${esc(message)}</span>`;
  const remove = () => el.remove();
  if (action) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn secondary sm';
    btn.textContent = action.label;
    btn.onclick = async () => { remove(); await action.run(); };
    el.appendChild(btn);
  }
  host.appendChild(el);
  while (host.children.length > 3) host.firstElementChild.remove();
  setTimeout(remove, action ? Math.max(ms, 8000) : ms);
}
const fail = (err) => { if (err && err.status !== 401) toast(err.message || String(err), { error: true }); };

/* A question with named answers. Resolves to the value picked, or null. */
function ask(title, body, choices, { danger = false } = {}) {
  const dlg = $('#ask');
  $('#ask-title').textContent = title;
  $('#ask-body').textContent = body;
  const actions = $('#ask-actions');
  actions.innerHTML = '';
  const cancel = document.createElement('button');
  cancel.className = 'btn ghost';
  cancel.value = '';
  cancel.textContent = 'Cancel';
  actions.appendChild(cancel);
  // A destructive question starts on Cancel, so Enter does not destroy.
  cancel.autofocus = danger;
  choices.forEach((c, i) => {
    const b = document.createElement('button');
    b.className = c.primary ? `btn${danger ? ' danger' : ''}` : 'btn secondary';
    b.value = c.value;
    b.textContent = c.label;
    if (!danger && (i === choices.length - 1 || c.primary)) b.autofocus = true;
    actions.appendChild(b);
  });
  return new Promise((resolve) => {
    dlg.onclose = () => resolve(dlg.returnValue || null);
    dlg.returnValue = '';
    dlg.showModal();
  });
}

/* ======================================================================
   State and names
   ====================================================================== */

const state = {
  settings: null, setup: null, home: null, shows: null, movies: null,
  calendar: null, jobs: null, history: null,
  libSource: 'sonarr', view: null,
  picked: new Set(), lastPicked: null, queueIds: [],
  sheet: null, labels: {},
};

try { state.libSource = localStorage.getItem('censarr.library_source') || localStorage.getItem('cleanarr.library_source') || 'sonarr'; } catch (e) { /* private window */ }
if (state.libSource === 'arr') state.libSource = 'sonarr';   // Sonarr + Radarr, before Radarr was dropped
try { state.hasRadarr = localStorage.getItem('censarr.radarr') === '1'; } catch (e) { /* private window */ }

function noteSource(data) {
  let s = data && (data.library_source || data.source);
  if (s === 'arr') s = 'sonarr';
  if (!['sonarr', 'plex', 'jellyfin'].includes(s)) return;
  state.libSource = s;
  // A listing says so outright; the settings carry the Radarr address itself.
  if (typeof data.radarr === 'boolean') state.hasRadarr = data.radarr;
  else if (data.radarr && typeof data.radarr === 'object') state.hasRadarr = !!(data.radarr.url && data.radarr.api_key);
  try {
    localStorage.setItem('censarr.library_source', s);
    localStorage.setItem('censarr.radarr', state.hasRadarr ? '1' : '');
  } catch (e) { /* ignore */ }
  // Upcoming has something to show with Sonarr's calendar or Radarr's.
  const upcoming = s === 'sonarr' || !!state.hasRadarr;
  $('#nav-upcoming').hidden = !upcoming;
  $('#more-upcoming').parentElement.hidden = !upcoming;
}

const serverName = () => ({ plex: 'Plex', jellyfin: 'Jellyfin' }[state.settings?.media_server] || 'the media server');
// Films always come from the media server; shows from Sonarr unless the
// media server is the whole library.
function sourceName(kind) {
  if (state.libSource === 'plex') return 'Plex';
  if (state.libSource === 'jellyfin') return 'Jellyfin';
  return kind === 'movie' ? serverName() : 'Sonarr';
}
const trackName = () => state.settings?.track_title || 'Censored - English';

function posterUrl(item, kind) {
  const src = item.source || (kind === 'movie' ? (state.settings?.media_server || 'none') : state.libSource);
  const id = item.id !== undefined && kind !== 'episode' ? item.id : item.series_id;
  // The size is part of the address, so a poster the browser holds is never
  // stale and never asked for again (see _poster_path on the server).
  return `/api/poster?source=${encodeURIComponent(src)}&id=${encodeURIComponent(id)}&w=400`;
}

/* The same rule as subtitles.worth_checking on the server. */
const LOW_CONFIDENCE = 0.5;
const unsure = (d) => d.confidence !== null && d.confidence !== undefined && d.confidence < LOW_CONFIDENCE;
const worthChecking = (d) => ['soundalike', 'subtitle_only'].includes(d.subtitle_state)
  || (!d.subtitle_state && unsure(d));

const DONE = ['done', 'skipped'];
const isDone = (status) => DONE.includes(status);
const STATUS = {
  done: ['done', 'Censored'], skipped: ['done', 'Nothing to mute'], running: ['running', 'Censoring'],
  queued: ['queued', 'Queued'], failed: ['failed', 'Failed'], cancelled: ['cancelled', 'Cancelled'],
};
const badge = (status) => {
  if (!status || !STATUS[status]) return '';
  const [cls, label] = STATUS[status];
  return `<span class="badge ${cls}">${label}</span>`;
};

/* ======================================================================
   Building blocks
   ====================================================================== */

const skeletonRows = (n = 4) => `<div class="rows" aria-hidden="true">${
  '<div class="skeleton row-sk"></div>'.repeat(n)}</div>`;
const skeletonPosters = (n = 12) => `<div class="posters" aria-hidden="true">${
  '<div class="skeleton poster-sk"></div>'.repeat(n)}</div>`;

function loading(note, shape) {
  return `<p class="loading-note" role="status">${esc(note)}</p>${shape}`;
}

function empty({ icon: name = 'info', title, text = '', actions = '' }) {
  return `<div class="empty">${icon(name)}<h3>${esc(title)}</h3>${
    text ? `<p>${text}</p>` : ''}${actions ? `<div class="actions">${actions}</div>` : ''}</div>`;
}

function problem(title, message, retryAction, { settings = true } = {}) {
  return `<div class="alert error" role="alert">${icon('alert')}<div class="grow">
    <strong>${esc(title)}</strong><span>${esc(message)}</span>
    <div class="actions">
      ${retryAction ? `<button type="button" class="btn secondary sm" data-action="${retryAction}">
        ${icon('refresh')}Try again</button>` : ''}
      ${settings ? '<a class="btn ghost sm" href="#/settings/library">Open settings</a>' : ''}
    </div></div></div>`;
}

/* Rewrite a live region without losing the keyboard's place: skipped when
   nothing changed, and focus is put back on the element with the same key. */
function morph(host, html) {
  if (host._html === html) return;
  const active = document.activeElement;
  const key = active && host.contains(active) ? active.dataset.key : null;
  host.innerHTML = html;
  host._html = html;
  if (key) {
    const again = host.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (again) again.focus({ preventScroll: true });
  }
}

function setBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  button.setAttribute('aria-busy', busy ? 'true' : 'false');
}

function posterCard(item, kind, index) {
  const chips = [];
  if (kind === 'show') {
    if (item.monitored) chips.push('<span class="badge auto">Auto</span>');
    if (item.pending) chips.push(`<span class="badge queued">${item.pending} queued</span>`);
    if (item.failed) chips.push(`<span class="badge failed">${item.failed} failed</span>`);
    if (item.cleaned) chips.push(`<span class="badge done">${item.cleaned}/${item.episodes} censored</span>`);
    if (!item.episodes) chips.push('<span class="badge">No files</span>');
  } else if (item.notYet) {
    chips.push('<span class="badge">Not downloaded</span>');
    if (item.marked) chips.push('<span class="badge auto">Will censor</span>');
  } else if (item.job_status) {
    chips.push(badge(item.job_status));
  }
  const line = kind === 'show'
    ? (item.episodes ? plural(item.episodes, 'episode') : 'nothing downloaded')
    : (item.notYet ? releaseLine(item) : (item.home ? ago(item.added) : (item.quality || '')));
  const action = kind === 'show' ? 'open-show' : (item.notYet ? 'open-wanted' : 'open-movie');
  const data = kind === 'show' || item.notYet
    ? `data-id="${esc(item.id)}" data-title="${esc(item.title)}"`
    : `data-index="${index}" data-list="${item.home ? 'home' : 'movies'}"`;
  const label = `${item.title}${item.year ? ` (${item.year})` : ''}${chips.length ? `, ${
    chips.map((c) => c.replace(/<[^>]+>/g, '')).join(', ')}` : ''}`;
  return `<article class="poster">
    <div class="art"><span class="initial" aria-hidden="true">${esc((item.title || '?').trim()[0] || '?')}</span>
      <img loading="lazy" decoding="async" alt="" src="${posterUrl(item, kind)}" onerror="this.remove()"></div>
    <div class="chips" aria-hidden="true">${chips.join('')}</div>
    <div class="meta">
      <button type="button" class="open" data-action="${action}" ${data}
        aria-label="${esc(label)}">${esc(item.title)}</button>
      <div class="year">${esc([item.year || '', line].filter(Boolean).join(' · '))}</div>
    </div></article>`;
}

/* ======================================================================
   Routing
   ====================================================================== */

const VIEWS = ['home', 'shows', 'movies', 'upcoming', 'queue', 'cleaned', 'settings'];
let settingsDirty = false;
let restoringHash = false;
let firstRoute = true;

function parseHash() {
  const [view, section] = (location.hash.replace(/^#\/?/, '') || 'home').split('/');
  return { view: VIEWS.includes(view) ? view : 'home', section: section || '' };
}

async function route() {
  if (restoringHash) { restoringHash = false; return; }
  const { view, section } = parseHash();
  const leaving = state.view;

  if (leaving === 'settings' && view !== 'settings' && settingsDirty) {
    const answer = await ask('Leave without saving?',
      'Your changes in Settings have not been saved.',
      [{ label: 'Discard changes', value: 'discard' }, { label: 'Save', value: 'save', primary: true }]);
    if (answer === 'save') {
      if (!(await saveSettings())) { restoringHash = true; location.hash = '#/settings'; return; }
    } else if (answer === 'discard') {
      discardSettings();
    } else {
      restoringHash = true;
      location.hash = '#/settings';
      return;
    }
  }

  state.view = view;
  $$('.view').forEach((v) => { v.hidden = v.id !== `view-${view}`; });
  $$('[data-nav]').forEach((a) => {
    if (a.dataset.nav === view) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  const title = $(`#view-${view}`).dataset.title;
  document.title = view === 'home' ? 'Cens-arr' : `${title} · Cens-arr`;
  if ($('#more-sheet').open) $('#more-sheet').close();

  if (!firstRoute && leaving !== view) {
    window.scrollTo(0, 0);
    $(`#view-${view} h1`).focus({ preventScroll: true });
  }
  firstRoute = false;

  if (view === 'home') loadHome();
  if (view === 'shows' && !state.shows) loadShows();
  if (view === 'movies' && !state.movies) loadMovies();
  if (view === 'upcoming') loadCalendar();
  if (view === 'queue') renderQueue();
  if (view === 'cleaned') loadCleaned();
  if (view === 'settings') {
    // A failure is already on the page, with a retry.
    try { await loadSettings(); } catch (err) { return; }
    if (section) {
      const panel = $(`#s-${section}`);
      if (panel) {
        panel.scrollIntoView({ block: 'start' });
        const heading = panel.querySelector('h2');
        heading.tabIndex = -1;
        heading.focus({ preventScroll: true });
      }
    }
  }
}
window.addEventListener('hashchange', route);

/* ======================================================================
   One listener for every click that does something
   ====================================================================== */

const ACTIONS = {};

document.addEventListener('click', async (event) => {
  // Clicking elsewhere closes an open overflow menu.
  $$('details.menu[open]').forEach((d) => { if (!d.contains(event.target)) d.open = false; });
  const el = event.target.closest('[data-action]');
  if (!el || el.tagName === 'INPUT' && el.type === 'checkbox' && el.dataset.action !== 'pick') return;
  const handler = ACTIONS[el.dataset.action];
  if (!handler) return;
  const menu = el.closest('details.menu');
  if (menu) menu.open = false;
  try {
    await handler(el, event);
  } catch (err) {
    fail(err);
  }
});

document.addEventListener('change', async (event) => {
  const el = event.target.closest('[data-change]');
  if (!el) return;
  const handler = ACTIONS[el.dataset.change];
  if (!handler) return;
  try { await handler(el, event); } catch (err) { fail(err); }
});

/* Overflow menus are opened here rather than by the browser, so a menu near
   the bottom of its scrolling area can be turned upwards before it is ever
   drawn: the browser's own toggle event comes a task later, after a frame of
   the menu hanging off the end. */
document.addEventListener('click', (event) => {
  const summary = event.target.closest('details.menu > summary');
  if (!summary) return;
  event.preventDefault();
  const menu = summary.parentElement;
  const opening = !menu.open;
  $$('details.menu[open]').forEach((d) => { if (d !== menu) d.open = false; });
  menu.classList.remove('up');
  menu.open = opening;
  if (!opening) return;
  const list = menu.querySelector('.menu-list');
  const area = menu.closest('.sheet-body') || document.documentElement;
  const bottom = Math.min(area.getBoundingClientRect().bottom, window.innerHeight);
  if (list.getBoundingClientRect().bottom > bottom - 8) menu.classList.add('up');
}, true);

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    $$('details.menu[open]').forEach((d) => {
      d.open = false;
      d.querySelector('summary').focus();
    });
  }
});

/* ======================================================================
   Queueing
   ====================================================================== */

async function queue(items, { monitor } = {}) {
  if (!items.length) return null;
  const body = { items };
  if (monitor) body.monitor = monitor;
  const result = await api('/api/jobs', { method: 'POST', body });
  const parts = [result.queued ? `Queued ${result.queued}` : 'Nothing new queued'];
  if (result.already_queued) parts.push(`${result.already_queued} already waiting`);
  toast(parts.join(' · '), { action: { label: 'Open queue', run: () => { location.hash = '#/queue'; } } });
  poll();
  return result;
}

/* Anything already censored is said out loud before it is censored again. */
async function confirmRedo(items) {
  const done = items.filter((i) => isDone(i.job_status));
  if (!done.length) return items;
  if (items.length === 1) {
    const one = done[0];
    const detail = [one.cleaned_at && `censored ${when(one.cleaned_at)}`,
      one.muted ? plural(one.muted, 'word') + ' muted' : 'nothing found to mute'].filter(Boolean).join(', ');
    const answer = await ask('Already censored',
      `${one.label} was done before (${detail}). Censoring it again replaces its censored track.`,
      [{ label: 'Censor it again', value: 'all', primary: true }]);
    return answer === 'all' ? items : [];
  }
  const answer = await ask('Some of these are already censored',
    `${done.length} of ${items.length} already have a censored track. Censoring them again replaces it.`,
    [{ label: 'All of them again', value: 'all' },
      { label: `Only the other ${items.length - done.length}`, value: 'rest', primary: true }]);
  if (answer === 'all') return items;
  if (answer === 'rest') return items.filter((i) => !isDone(i.job_status));
  return [];
}

const episodeJob = (e, showTitle, extra = {}) => ({
  kind: 'episode', title: showTitle,
  subtitle: `${epCode(e.season, e.episode)} · ${e.title || ''}`,
  path: e.path, source: 'sonarr', source_id: String(e.id ?? e.episode_id), ...extra,
});

async function removeTracks(items, label, toJob) {
  const cleaned = items.filter((e) => isDone(e.job_status));
  if (!cleaned.length) { toast('None of those have a censored track'); return; }
  const freed = cleaned.reduce((sum, e) => sum + (e.added_bytes || 0), 0);
  const answer = await ask(
    `Remove the censored track from ${plural(cleaned.length, 'file')}?`,
    `${label}. Only the “${trackName()}” track is taken out; the original audio is untouched`
    + `${freed ? `, and about ${size(freed)} comes back` : ''}.`,
    [{ label: 'Remove', value: 'yes', primary: true }], { danger: true });
  if (answer !== 'yes') return;
  await queue(cleaned.map(toJob));
}

/* ======================================================================
   Status everywhere: the sidebar, the nav counts, the "now" cards
   ====================================================================== */

async function poll() {
  let data;
  try { data = await api('/api/jobs'); } catch (err) { return; }
  state.jobs = data;
  const { stats } = data;
  const waiting = stats.waiting || 0;
  $('#queue-count').textContent = waiting ? String(waiting) : '';
  $('#cleaned-count').textContent = stats.cleaned_files ? String(stats.cleaned_files) : '';
  $('#more-cleaned-count').textContent = stats.cleaned_files ? String(stats.cleaned_files) : '';
  renderSideStatus();
  if (state.view === 'queue') renderQueue();
  if (state.view === 'home') renderHomeNow();
}

function runningJob() {
  return (state.jobs?.items || []).find((j) => j.status === 'running') || null;
}

function renderSideStatus() {
  const data = state.jobs;
  if (!data) return;
  const job = runningJob();
  let html;
  if (data.holding) {
    html = `<strong><span class="pulse hold"></span>Waiting for ${esc(serverName())}</strong>
      <span>${esc(data.holding)}</span>`;
  } else if (job) {
    html = `<strong><span class="pulse busy"></span>Censoring ${Math.round(job.progress * 100)}%</strong>
      <span>${esc(job.title)} ${esc(job.subtitle || '')}</span>`;
  } else {
    const waiting = data.stats.waiting || 0;
    html = `<strong><span class="pulse idle"></span>${waiting ? plural(waiting, 'job') + ' waiting' : 'Idle'}</strong>`;
  }
  morph($('#side-status'), html);
}

/* What is happening right now: a job, a hold, or nothing. */
function nowCard({ withCancel = false } = {}) {
  const data = state.jobs;
  if (!data) return '';
  const job = runningJob();
  if (data.holding) {
    return `<div class="card now hold" role="status">${icon('pause', 'i big')}
      <div class="grow"><div class="title">Waiting while ${esc(serverName())} is busy</div>
        <div class="sub">${esc(data.holding)}. The queue carries on when it stops.</div></div>
      <button type="button" class="btn secondary sm" data-action="clean-anyway" data-key="anyway">Censor anyway</button></div>`;
  }
  if (!job) return '';
  const pct = Math.round((job.progress || 0) * 100);
  return `<div class="card now">${icon('wave', 'i big')}
    <div class="grow">
      <div class="title">${esc(job.title)} <span class="muted">${esc(job.subtitle || '')}</span></div>
      <div class="sub">${esc(job.message || job.stage || 'starting')}${
        job.started_at ? ` · ${elapsed(job.started_at)}` : ''}</div>
      <div class="progress" role="progressbar" aria-label="Progress" aria-valuemin="0" aria-valuemax="100"
        aria-valuenow="${pct}"><span style="width:${pct}%"></span></div>
    </div>
    <b>${pct}%</b>
    ${withCancel ? `<button type="button" class="btn ghost sm" data-action="cancel-job" data-id="${job.id}" data-key="cancel-${job.id}">Cancel</button>` : ''}
  </div>`;
}

ACTIONS['clean-anyway'] = async () => {
  await api('/api/queue/clean-anyway', { method: 'POST' });
  toast(`Carrying on while ${serverName()} is busy`);
  poll();
};

/* ======================================================================
   Home
   ====================================================================== */

const SECTION_LINKS = {
  library: '#/settings/library', listening: '#/settings/listening', shows: '#/shows',
  security: '#/settings/security', judge: '#/settings/judge',
};

async function loadSetup() {
  try {
    state.setup = await api('/api/setup');
  } catch (err) { return; }
  const done = state.setup.done;
  $('#settings-dot').hidden = done;
  $('#more-dot').hidden = done;
  $('#more-settings-dot').hidden = done;
  const worst = (keys) => {
    const items = state.setup.required.concat(state.setup.optional).filter((i) => keys.includes(i.key));
    for (const s of ['problem', 'todo', 'info']) if (items.some((i) => i.state === s)) return s;
    return items.length ? 'ok' : '';
  };
  $$('[data-state]').forEach((el) => {
    const keys = { library: ['library', 'paths'], listening: ['listening'], login: ['login'] }[el.dataset.state];
    el.dataset.value = worst(keys);
    el.title = { ok: 'Done', todo: 'Needs setting up', problem: 'Has a problem', info: 'Optional' }[el.dataset.value] || '';
  });
  if (state.view === 'home') renderSetup();
}

function renderSetup() {
  const host = $('#home-setup');
  const setup = state.setup;
  if (!setup || setup.done) { host.innerHTML = ''; return; }
  const ok = setup.required.filter((i) => i.state === 'ok').length;
  const item = (i, n) => `<li class="${i.state}">
    <span class="mark" aria-hidden="true">${i.state === 'ok' ? icon('check')
      : (i.state === 'problem' ? '!' : (i.state === 'info' ? 'i' : n))}</span>
    <div class="grow"><strong>${esc(i.title)}</strong>
      <span class="sr-only">${{ ok: 'done', todo: 'to do', problem: 'problem', info: 'optional' }[i.state]}.</span>
      <div class="detail">${esc(i.detail)}</div></div>
    ${i.state !== 'ok' && SECTION_LINKS[i.section]
      ? `<a class="btn secondary sm go" href="${SECTION_LINKS[i.section]}">${
        i.key === 'first_clean' ? 'Pick one' : 'Fix'}</a>` : ''}
  </li>`;
  host.innerHTML = `<section class="card setup" aria-labelledby="setup-h">
    <div class="setup-head">
      <h2 id="setup-h">Finish setting up</h2>
      <span class="muted small">${ok} of ${setup.required.length} done</span>
      <div class="meter" aria-hidden="true"><span style="width:${Math.round(100 * ok / setup.required.length)}%"></span></div>
    </div>
    <ol class="checklist">${setup.required.map((i, n) => item(i, n + 1)).join('')}</ol>
    ${setup.optional.some((i) => i.state !== 'ok') ? `<p class="optional-head">Optional</p>
      <ul class="checklist">${setup.optional.filter((i) => i.state !== 'ok').map((i) => item(i, '')).join('')}</ul>` : ''}
  </section>`;
}

function renderHomeNow() {
  morph($('#home-now'), nowCard());
}

async function loadHome() {
  loadSetup();
  renderHomeNow();
  if (!state.home) {
    $('#home-stats').innerHTML = '<div class="skeleton stat-sk"></div>'.repeat(4);
    $('#home-episodes').innerHTML = loading(`Asking ${sourceName('show')}…`, skeletonRows(4));
    $('#home-movies').innerHTML = loading(`Asking ${sourceName('movie')}…`, skeletonPosters(6));
  }
  let data;
  try {
    data = await api('/api/home');
  } catch (err) {
    if (err.status === 401) return;
    $('#home-stats').innerHTML = '';
    $('#home-episodes').innerHTML = problem('Could not load Home', err.message, 'reload-home');
    $('#home-movies').innerHTML = '';
    return;
  }
  noteSource(data);
  state.home = data;
  renderHome();
  prefetchLibrary();
}
ACTIONS['reload-home'] = () => { state.home = null; return loadHome(); };

/* With Home drawn, the show and film lists are fetched quietly, so opening
   either tab shows them at once rather than waiting on the library. */
function prefetchLibrary() {
  if (state.prefetched) return;
  state.prefetched = true;
  const later = window.requestIdleCallback || ((fn) => setTimeout(fn, 800));
  later(() => {
    if (!state.shows) loadShows();
    if (!state.movies) loadMovies();
  });
}

function renderHome() {
  const data = state.home;
  if (!data) return;
  const { stats } = data;
  const tile = (value, label, href) => (href
    ? `<a class="stat" href="${href}"><b>${value}</b><span>${label}</span></a>`
    : `<div class="stat"><b>${value}</b><span>${label}</span></div>`);
  $('#home-stats').innerHTML =
    tile(stats.cleaned_files, stats.cleaned_files === 1 ? 'file censored' : 'files censored', '#/cleaned')
    + tile(stats.words_muted, stats.words_muted === 1 ? 'word muted' : 'words muted')
    + tile(size(stats.added_bytes) || '0 MB', 'of censored audio', '#/cleaned')
    + tile(data.queued + data.running, 'in the queue', '#/queue')
    + tile(data.monitors, data.monitors === 1 ? 'show censoring new episodes' : 'shows censoring new episodes', '#/shows')
    + (data.failed ? tile(data.failed, 'failed', '#/queue') : '');

  $('#home-problems').innerHTML = data.problems.length
    ? `<div class="alert warn">${icon('alert')}<div class="grow"><strong>Not everything answered</strong>
        ${data.problems.map((p) => `<div>${esc(p)}</div>`).join('')}
        <div class="actions"><button type="button" class="btn secondary sm" data-action="reload-home">${icon('refresh')}Try again</button>
          <a class="btn ghost sm" href="#/settings/library">Open settings</a></div></div></div>`
    : '';

  $('#home-eps-note').textContent = `newest first, from ${sourceName('show')}`;
  $('#home-films-note').textContent = `newest first, from ${sourceName('movie')}`;

  $('#home-episodes').innerHTML = data.episodes.length
    ? `<div class="rows">${data.episodes.map((e, i) => `
      <div class="row">
        <img class="thumb" loading="lazy" decoding="async" alt="" src="${posterUrl(e, 'episode')}" onerror="this.classList.add('none')">
        <div class="grow">
          <div class="title">${esc(e.series)} <span class="muted">${epCode(e.season, e.episode)}${
            e.title ? ` · ${esc(e.title)}` : ''}</span></div>
          <div class="sub">${esc([ago(e.added), e.quality, size(e.size),
            e.muted ? plural(e.muted, 'word') + ' muted' : ''].filter(Boolean).join(' · '))}</div>
        </div>
        <div class="actions">
          ${badge(e.job_status)}${e.monitored ? '<span class="badge auto">Auto</span>' : ''}
          <button type="button" class="btn ghost sm" data-action="open-show" data-id="${esc(e.series_id)}"
            data-title="${esc(e.series)}">Open show</button>
          <button type="button" class="btn sm" data-action="clean-home-episode" data-index="${i}"
            aria-label="${isDone(e.job_status) ? 'Censor again' : 'Censor'}: ${esc(e.series)} ${epCode(e.season, e.episode)}"
            ${['queued', 'running'].includes(e.job_status) ? 'disabled' : ''}>${isDone(e.job_status) ? 'Censor again' : 'Censor'}</button>
        </div>
      </div>`).join('')}</div>`
    : empty({ icon: 'tv', title: 'Nothing new lately',
      text: `${esc(sourceName('show'))} has not reported any new episodes.` });

  $('#home-movies').innerHTML = data.movies.length
    ? `<div class="posters">${data.movies.map((m, i) => posterCard({ ...m, home: true }, 'movie', i)).join('')}</div>`
    : empty({ icon: 'film', title: 'No films yet',
      text: `${esc(sourceName('movie'))} has not reported any films.` });
}

ACTIONS['clean-home-episode'] = async (el) => {
  const e = state.home.episodes[Number(el.dataset.index)];
  if (!e) return;
  const wanted = await confirmRedo([{ ...e, label: `${e.series} ${epCode(e.season, e.episode)}` }]);
  if (!wanted.length) return;
  setBusy(el, true);
  try {
    await queue([episodeJob({ ...e, id: e.episode_id }, e.series, { force: isDone(e.job_status) })]);
  } finally { setBusy(el, false); }
  state.home = null;
  loadHome();
};

/* ======================================================================
   Shows and movies
   ====================================================================== */

const byRecent = (a, b) => String(b.latest || '').localeCompare(String(a.latest || ''));
const byTitle = (a, b) => (a.title || '').toLowerCase().localeCompare((b.title || '').toLowerCase());

/* One request at a time per list: the quiet fetch from Home and a click on
   the tab share it. */
const inFlight = {};
const once = (name, fn) => () => {
  inFlight[name] ||= fn().finally(() => { inFlight[name] = null; });
  return inFlight[name];
};

const loadShows = once('shows', async () => {
  $('#show-grid').innerHTML = loading(`Asking ${sourceName('show')}…`, skeletonPosters(18));
  $('#show-count').textContent = '';
  try {
    const data = await api('/api/series');
    noteSource(data);
    state.shows = data.items;
    renderShows();
  } catch (err) {
    if (err.status === 401) return;
    state.shows = null;
    $('#show-grid').innerHTML = problem(`Could not list your shows`, err.message, 'reload-shows');
  }
});
ACTIONS['reload-shows'] = loadShows;

function renderShows() {
  if (!state.shows) return;
  const needle = $('#show-search').value.trim().toLowerCase();
  const filter = $('#show-filter').value;
  const items = state.shows.filter((s) => {
    if (needle && !(s.title || '').toLowerCase().includes(needle)) return false;
    if (filter === 'cleaned') return s.cleaned > 0;
    if (filter === 'monitored') return s.monitored;
    if (filter === 'untouched') return !s.cleaned && !s.pending;
    if (filter === 'nofiles') return !s.episodes;
    return true;
  }).sort($('#show-sort').value === 'recent' ? byRecent : byTitle);
  $('#show-count').textContent = `${plural(items.length, 'show')}${
    items.length !== state.shows.length ? ` of ${state.shows.length}` : ''}`;
  if (!state.shows.length) {
    $('#show-grid').innerHTML = empty({ icon: 'tv', title: 'No shows yet',
      text: `${esc(sourceName('show'))} answered, but has no shows.`,
      actions: '<a class="btn secondary sm" href="#/settings/library">Check the library settings</a>' });
    return;
  }
  $('#show-grid').innerHTML = items.length
    ? `<div class="posters">${items.map((s) => posterCard(s, 'show')).join('')}</div>`
    : empty({ icon: 'search', title: 'Nothing matches',
      actions: '<button type="button" class="btn secondary sm" data-action="clear-show-filters">Clear the search</button>' });
}
ACTIONS['clear-show-filters'] = () => {
  $('#show-search').value = ''; $('#show-filter').value = 'all'; renderShows(); $('#show-search').focus();
};
$('#show-search').addEventListener('input', debounce(renderShows, 120));
$('#show-filter').addEventListener('change', renderShows);
$('#show-sort').addEventListener('change', renderShows);

const loadMovies = once('movies', async () => {
  $('#movie-grid').innerHTML = loading(`Asking ${sourceName('movie')}…`, skeletonPosters(18));
  $('#movie-count').textContent = '';
  try {
    const data = await api('/api/movies');
    noteSource(data);
    state.movies = data.items;
    $('#films-auto').checked = !!data.auto;
    renderMovies();
    if (state.hasRadarr && !state.wanted) loadWanted();
  } catch (err) {
    if (err.status === 401) return;
    state.movies = null;
    $('#movie-grid').innerHTML = problem('Could not list your films', err.message, 'reload-movies');
  }
});
ACTIONS['reload-movies'] = loadMovies;

/* Films Radarr has no file for yet. Fetched only when asked for. */
const loadWanted = once('wanted', async () => {
  // Only the Radarr-only view waits on this; "All movies" is already drawn
  // and gains the extra cards when the answer comes.
  if ($('#movie-filter').value === 'wanted') {
    $('#movie-grid').innerHTML = loading('Asking Radarr…', skeletonRows(5));
  }
  try {
    const data = await api('/api/radarr/wanted');
    state.wanted = data;
  } catch (err) {
    if (err.status === 401) return;
    state.wanted = null;
    $('#movie-grid').innerHTML = problem('Could not ask Radarr', err.message, 'reload-wanted');
    return;
  }
  renderMovies();      // cheap, and right even if fetched behind another page
});
ACTIONS['reload-wanted'] = () => { state.wanted = null; loadWanted(); };

const releaseLine = (m) => {
  if (!m.released) return m.status === 'announced' ? 'Announced, no release date yet' : '';
  const soon = m.released >= new Date().toISOString().slice(0, 10);
  const day = new Date(`${m.released}T00:00:00`).toLocaleDateString(undefined,
    { day: 'numeric', month: 'short', year: 'numeric' });
  return `${soon ? 'Out' : 'Came out'} ${m.release_kind} ${day}`;
};

function renderWanted() {
  const data = state.wanted;
  if (!data) { loadWanted(); return; }
  if (data.unavailable) {
    $('#movie-count').textContent = '';
    $('#movie-grid').innerHTML = empty({ icon: 'film', title: 'Radarr is not set up',
      text: 'Add Radarr to mark films that have not downloaded yet, so they are censored as they arrive.',
      actions: '<a class="btn secondary sm" href="#/settings/library">Add Radarr</a>' });
    return;
  }
  const needle = $('#movie-search').value.trim().toLowerCase();
  const items = data.items.filter((m) => !needle || (m.title || '').toLowerCase().includes(needle));
  if ($('#movie-sort').value === 'title') items.sort(byTitle);
  else items.sort((a, b) => String(b.added || '').localeCompare(String(a.added || '')));
  const marked = data.items.filter((m) => m.marked).length;
  $('#movie-count').textContent = `${plural(items.length, 'film')} not downloaded yet${
    marked ? ` · ${marked} to censor` : ''}`;
  if (!data.items.length) {
    $('#movie-grid').innerHTML = empty({ icon: 'check', title: 'Nothing waiting',
      text: 'Radarr has a file for every film it knows about.' });
    return;
  }
  $('#movie-grid').innerHTML = items.length ? `<div class="rows">${items.map((m) => `
    <div class="row">
      <img class="thumb" loading="lazy" decoding="async" alt="" src="${posterUrl(m, 'movie')}" onerror="this.classList.add('none')">
      <div class="grow">
        <div class="title">${esc(m.title)} ${m.year ? `<span class="muted">${esc(m.year)}</span>` : ''}</div>
        <div class="sub">${esc([releaseLine(m), m.wanted ? '' : 'Radarr is not looking for it'].filter(Boolean).join(' · '))}</div>
      </div>
      <div class="actions">
        <label class="switch"><input type="checkbox" role="switch" data-change="want-film" data-id="${esc(m.id)}"
          data-title="${esc(m.title)}" ${m.marked ? 'checked' : ''}>
          <span class="track" aria-hidden="true"></span><span>Censor when it downloads<span class="sr-only">: ${esc(m.title)}</span></span></label>
      </div>
    </div>`).join('')}</div>`
    : empty({ icon: 'search', title: 'Nothing matches',
      actions: '<button type="button" class="btn secondary sm" data-action="clear-movie-filters">Clear the search</button>' });
}

/* Mark one film Radarr is waiting for: it is queued when its file arrives. */
ACTIONS['want-film'] = async (box) => {
  const { id, title } = box.dataset;
  const on = box.checked;
  box.disabled = true;
  try {
    if (on) {
      await api('/api/monitors', { method: 'POST',
        body: { source: 'radarr', source_id: String(id), title, mode: 'new_only' } });
      toast(`${title} will be censored when it downloads`);
    } else {
      await api(`/api/monitors/radarr/${encodeURIComponent(id)}`, { method: 'DELETE' });
      toast(`${title} will not be censored automatically`);
    }
  } catch (err) {
    box.checked = !on;
    throw err;
  } finally {
    box.disabled = false;
  }
  const film = (state.wanted?.items || []).find((m) => String(m.id) === String(id));
  if (film) film.marked = on;
  (state.calendar || []).forEach((e) => { if (e.kind === 'film' && String(e.id) === String(id)) e.marked = on; });
  if (state.view === 'movies') renderMovies();
  if (state.view === 'upcoming') renderCalendar();
};

/* The films Radarr is still waiting for, as poster cards for "All movies".
   One already listed by the media server (same title and year) is left out. */
function wantedCards() {
  const have = new Set((state.movies || []).map((m) => `${(m.title || '').toLowerCase()}|${m.year || ''}`));
  return (state.wanted?.items || [])
    .filter((m) => !have.has(`${(m.title || '').toLowerCase()}|${m.year || ''}`))
    .map((m) => ({ ...m, notYet: true }));
}

function renderMovies() {
  if ($('#movie-filter').value === 'wanted') { renderWanted(); return; }
  if (!state.movies) return;
  const needle = $('#movie-search').value.trim().toLowerCase();
  const filter = $('#movie-filter').value;
  const waiting = filter === 'all' ? wantedCards() : [];
  const total = state.movies.length + waiting.length;
  const items = [...state.movies.map((m, index) => ({ ...m, index })), ...waiting].filter((m) => {
    if (needle && !(m.title || '').toLowerCase().includes(needle)) return false;
    if (filter === 'cleaned') return isDone(m.job_status);
    if (filter === 'untouched') return !m.job_status;
    return true;
  }).sort($('#movie-sort').value === 'recent' ? byRecent : byTitle);
  const notYet = items.filter((m) => m.notYet).length;
  $('#movie-count').textContent = `${plural(items.length, 'film')}${
    items.length !== total ? ` of ${total}` : ''}${notYet ? ` · ${notYet} not downloaded yet` : ''}`;
  if (!state.movies.length) {
    $('#movie-grid').innerHTML = empty({ icon: 'film', title: 'No films yet',
      text: `${esc(sourceName('movie'))} answered, but has no films with a file.`,
      actions: '<a class="btn secondary sm" href="#/settings/library">Check the library settings</a>' });
    return;
  }
  $('#movie-grid').innerHTML = items.length
    ? `<div class="posters">${items.map((m) => posterCard(m, 'movie', m.index)).join('')}</div>`
    : empty({ icon: 'search', title: 'Nothing matches',
      actions: '<button type="button" class="btn secondary sm" data-action="clear-movie-filters">Clear the search</button>' });
}
ACTIONS['clear-movie-filters'] = () => {
  $('#movie-search').value = ''; $('#movie-filter').value = 'all'; renderMovies(); $('#movie-search').focus();
};
$('#movie-search').addEventListener('input', debounce(renderMovies, 120));
$('#movie-filter').addEventListener('change', renderMovies);
$('#movie-sort').addEventListener('change', renderMovies);

/* ======================================================================
   The sheet: a show, a film, or a job
   ====================================================================== */

function openSheet(title, sub = '') {
  const sheet = $('#sheet');
  $('#sheet-title').textContent = title;
  $('#sheet-sub').textContent = sub;
  $('#sheet-actions').innerHTML = '';
  $('#sheet-body').innerHTML = '';
  $('#sheet-body').scrollTop = 0;
  if (!sheet.open) sheet.showModal();
  return sheet;
}
function closeSheet() { const s = $('#sheet'); if (s.open) s.close(); state.sheet = null; }
$('#sheet-close').addEventListener('click', closeSheet);
$('#sheet').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeSheet(); });
$('#sheet').addEventListener('close', () => { state.sheet = null; });

/* ---------------------------------------------------------------- a show */

ACTIONS['open-show'] = (el) => openShow(el.dataset.id, el.dataset.title);

async function openShow(id, title) {
  state.sheet = { kind: 'show', id, title, episodes: null, open: new Set() };
  openSheet(title);
  renderShowActions();
  $('#sheet-body').innerHTML = loading('Loading episodes…', skeletonRows(5));
  try {
    const { items } = await api(`/api/series/${encodeURIComponent(id)}/episodes`);
    if (state.sheet?.id !== id) return;
    state.sheet.episodes = items;
    const seasons = [...new Set(items.map((e) => e.season))];
    if (seasons.length === 1) state.sheet.open.add(seasons[0]);
  } catch (err) {
    if (state.sheet?.id !== id) return;
    $('#sheet-body').innerHTML = problem('Could not load the episodes', err.message, 'reload-show', { settings: false });
    return;
  }
  renderShow();
}
ACTIONS['reload-show'] = () => openShow(state.sheet.id, state.sheet.title);

function isWatched(id) {
  const show = (state.shows || []).find((s) => String(s.id) === String(id));
  if (show) return !!show.monitored;
  return !!state.sheet?.monitored;
}

async function renderShowActions() {
  const sheet = state.sheet;
  if (!sheet || sheet.kind !== 'show') return;
  if (!(state.shows || []).some((s) => String(s.id) === String(sheet.id))) {
    try {
      const { items } = await api('/api/monitors');
      sheet.monitored = items.some((m) => m.source === 'sonarr' && String(m.source_id) === String(sheet.id));
    } catch (err) { /* the switch shows off */ }
  }
  const eps = sheet.episodes || [];
  const outstanding = eps.filter((e) => !isDone(e.job_status) && !['queued', 'running'].includes(e.job_status));
  const cleaned = eps.filter((e) => isDone(e.job_status));
  $('#sheet-sub').textContent = sheet.episodes
    ? `${plural(eps.length, 'episode')} on disk · ${cleaned.length} censored` : '';
  $('#sheet-actions').innerHTML = `
    <label class="switch" title="Episodes downloaded from now on are censored when they arrive. Nothing already on disk is touched.">
      <input type="checkbox" role="switch" data-change="watch" data-id="${esc(sheet.id)}"
        data-title="${esc(sheet.title)}" ${isWatched(sheet.id) ? 'checked' : ''}>
      <span class="track" aria-hidden="true"></span><span>Censor new episodes automatically</span></label>
    ${outstanding.length ? `<button type="button" class="btn sm" data-action="clean-outstanding">
      Censor ${outstanding.length === eps.length ? 'all' : `${outstanding.length} not censored yet`}</button>` : ''}
    ${cleaned.length ? `<button type="button" class="btn ghost sm" data-action="remove-show">Remove censored tracks</button>` : ''}`;
}

function renderShow() {
  const sheet = state.sheet;
  if (!sheet || sheet.kind !== 'show') return;
  renderShowActions();
  const eps = sheet.episodes || [];
  if (!eps.length) {
    $('#sheet-body').innerHTML = empty({ icon: 'tv', title: 'Nothing on disk yet',
      text: `${esc(sourceName('show'))} has no episode files for this show, so there is nothing to censor today.
        Switch on <strong>Censor new episodes automatically</strong> and each one is censored as it arrives.` });
    return;
  }
  const seasons = [...new Set(eps.map((e) => e.season))].sort((a, b) => a - b);
  $('#sheet-body').innerHTML = seasons.map((season) => {
    const list = eps.filter((e) => e.season === season);
    const clean = list.filter((e) => isDone(e.job_status)).length;
    const busy = list.filter((e) => ['queued', 'running'].includes(e.job_status)).length;
    const open = sheet.open.has(season);
    const name = season === 0 ? 'Specials' : `Season ${season}`;
    return `<section class="season">
      <div class="season-head">
        <button type="button" class="season-toggle" aria-expanded="${open}" aria-controls="season-${season}"
          data-action="toggle-season" data-season="${season}">${icon('right')}
          <strong>${name}</strong><span>${clean} of ${list.length} censored${busy ? ` · ${busy} queued` : ''}</span></button>
        <button type="button" class="btn secondary sm" data-action="clean-season" data-season="${season}"
          aria-label="Censor ${name}">Censor season</button>
        ${clean ? `<button type="button" class="btn ghost sm" data-action="remove-season" data-season="${season}"
          aria-label="Remove censored tracks from ${name}">Remove censored</button>` : ''}
      </div>
      <div class="season-body" id="season-${season}" ${open ? '' : 'hidden'}>
        ${list.map((e) => episodeRow(e)).join('')}
      </div></section>`;
  }).join('');
}

function episodeRow(e) {
  const label = `${epCode(e.season, e.episode)}${e.title ? ` ${e.title}` : ''}`;
  const busy = ['queued', 'running'].includes(e.job_status);
  return `<div class="row">
    <div class="grow">
      <div class="title">${pad2(e.episode)}. ${esc(e.title || 'Untitled')}</div>
      <div class="sub">${esc([e.quality, size(e.size), e.muted ? plural(e.muted, 'word') + ' muted' : '',
        e.added_bytes ? `+${size(e.added_bytes)}` : '', e.cleaned_at ? when(e.cleaned_at) : ''].filter(Boolean).join(' · '))}</div>
    </div>
    <div class="actions">
      ${badge(e.job_status)}
      <button type="button" class="btn sm${isDone(e.job_status) ? ' secondary' : ''}" data-action="clean-episode"
        data-id="${esc(e.id)}" aria-label="${isDone(e.job_status) ? 'Censor again' : 'Censor'}: ${esc(label)}" ${busy ? 'disabled' : ''}>
        ${isDone(e.job_status) ? 'Censor again' : 'Censor'}</button>
      <details class="menu">
        <summary class="icon-btn" aria-label="More for ${esc(label)}">${icon('more')}</summary>
        <div class="menu-list">
          <button type="button" data-action="clean-from" data-id="${esc(e.id)}">${icon('arrow')}Censor this and every one after it</button>
          ${e.job_id ? `<button type="button" data-action="open-job" data-id="${e.job_id}">${icon('info')}What was muted</button>` : ''}
          ${isDone(e.job_status) ? `<button type="button" data-action="remove-episode" data-id="${esc(e.id)}">${icon('trash')}Remove the censored track</button>` : ''}
        </div>
      </details>
    </div></div>`;
}

ACTIONS['toggle-season'] = (el) => {
  const season = Number(el.dataset.season);
  const open = state.sheet.open;
  if (open.has(season)) open.delete(season); else open.add(season);
  const expanded = open.has(season);
  el.setAttribute('aria-expanded', String(expanded));
  $(`#season-${season}`).hidden = !expanded;
};

async function cleanEpisodes(list) {
  const sheet = state.sheet;
  const labelled = list.map((e) => ({ ...e, label: `${epCode(e.season, e.episode)} ${e.title || ''}`.trim() }));
  const wanted = await confirmRedo(labelled);
  if (!wanted.length) return;
  await queue(wanted.map((e) => episodeJob(e, sheet.title, { force: isDone(e.job_status) })),
    { monitor: { source: 'sonarr', source_id: String(sheet.id), title: sheet.title } });
  wanted.forEach((w) => {
    const found = sheet.episodes.find((e) => e.id === w.id);
    if (found) found.job_status = 'queued';
  });
  // Censoring a season marks the show to censor what arrives from now on.
  const show = (state.shows || []).find((s) => String(s.id) === String(sheet.id));
  if (show) show.monitored = true;
  sheet.monitored = true;
  renderShow();
}

const sheetEpisode = (id) => state.sheet.episodes.find((e) => String(e.id) === String(id));
ACTIONS['clean-episode'] = (el) => cleanEpisodes([sheetEpisode(el.dataset.id)]);
ACTIONS['clean-season'] = (el) => cleanEpisodes(state.sheet.episodes.filter((e) => e.season === Number(el.dataset.season)));
ACTIONS['clean-from'] = (el) => {
  const start = sheetEpisode(el.dataset.id);
  return cleanEpisodes(state.sheet.episodes.filter((e) => e.season > start.season
    || (e.season === start.season && e.episode >= start.episode)));
};
ACTIONS['clean-outstanding'] = async () => {
  const outstanding = state.sheet.episodes.filter((e) => !isDone(e.job_status) && !['queued', 'running'].includes(e.job_status));
  const minutes = outstanding.length * 2.5;
  const answer = await ask(`Censor ${plural(outstanding.length, 'episode')}?`,
    `Everything on disk for ${state.sheet.title} that is not censored yet. Roughly ${
      minutes < 60 ? `${Math.round(minutes)} minutes` : `${(minutes / 60).toFixed(1)} hours`} with a GPU; longer on a CPU.`,
    [{ label: 'Queue them', value: 'yes', primary: true }]);
  if (answer === 'yes') await cleanEpisodes(outstanding);
};
const removeEpisodeJob = (e) => episodeJob(e, state.sheet.title, {
  subtitle: `${epCode(e.season, e.episode)} · ${e.title || ''} (removing)`, action: 'remove' });
ACTIONS['remove-episode'] = (el) => {
  const e = sheetEpisode(el.dataset.id);
  return removeTracks([e], `${state.sheet.title} ${epCode(e.season, e.episode)}`, removeEpisodeJob);
};
ACTIONS['remove-season'] = (el) => {
  const season = Number(el.dataset.season);
  return removeTracks(state.sheet.episodes.filter((e) => e.season === season),
    `${state.sheet.title}, season ${season}`, removeEpisodeJob);
};
ACTIONS['remove-show'] = () => removeTracks(state.sheet.episodes,
  `Every censored episode of ${state.sheet.title}`, removeEpisodeJob);

/* The switch that marks a show to censor what arrives. Same wherever it is. */
ACTIONS.watch = async (box) => {
  const { id, title } = box.dataset;
  const on = box.checked;
  box.disabled = true;
  try {
    if (on) {
      await api('/api/monitors', { method: 'POST',
        body: { source: 'sonarr', source_id: String(id), title, mode: 'new_only' } });
      toast(`${title}: new episodes will be censored as they arrive`);
    } else {
      await api(`/api/monitors/sonarr/${encodeURIComponent(id)}`, { method: 'DELETE' });
      toast(`${title}: new episodes no longer censored automatically`);
    }
  } catch (err) {
    box.checked = !on;
    throw err;
  } finally {
    box.disabled = false;
  }
  const show = (state.shows || []).find((s) => String(s.id) === String(id));
  if (show) show.monitored = on;
  if (state.sheet && String(state.sheet.id) === String(id)) state.sheet.monitored = on;
  (state.calendar || []).forEach((e) => { if (String(e.series_id) === String(id)) e.monitored = on; });
  if (state.view === 'shows') renderShows();
  if (state.view === 'upcoming') renderCalendar();
};

/* The same switch for films: one for the whole library, since a film has no
   show to mark. Films the media server adds from now on are queued. */
ACTIONS['watch-films'] = async (box) => {
  const on = box.checked;
  box.disabled = true;
  try {
    if (on) {
      await api('/api/monitors', { method: 'POST',
        body: { source: 'films', source_id: 'new', title: 'New films', mode: 'new_only' } });
      toast('New films will be censored as they arrive');
    } else {
      await api('/api/monitors/films/new', { method: 'DELETE' });
      toast('New films no longer censored automatically');
    }
  } catch (err) {
    box.checked = !on;
    throw err;
  } finally {
    box.disabled = false;
  }
};

/* ---------------------------------------------------------------- a film */

ACTIONS['open-wanted'] = (el) => {
  const m = (state.wanted?.items || []).find((f) => String(f.id) === el.dataset.id);
  if (!m) return;
  state.sheet = { kind: 'wanted', id: m.id };
  openSheet(m.title, [m.year, releaseLine(m)].filter(Boolean).join(' · '));
  $('#sheet-actions').innerHTML = `<label class="switch"><input type="checkbox" role="switch" data-change="want-film"
      data-id="${esc(m.id)}" data-title="${esc(m.title)}" ${m.marked ? 'checked' : ''}>
    <span class="track" aria-hidden="true"></span><span>Censor when it downloads</span></label>`;
  $('#sheet-body').innerHTML = `<p>Radarr has no file for this film yet${m.wanted ? '' : ', and is not looking for one'}.
    Switch on <strong>Censor when it downloads</strong> and it is censored the moment it arrives.</p>`;
};

ACTIONS['open-movie'] = (el) => {
  const list = el.dataset.list === 'home' ? state.home.movies : state.movies;
  const m = list[Number(el.dataset.index)];
  if (m) openMovie(m);
};

function openMovie(m) {
  state.sheet = { kind: 'movie', movie: m };
  openSheet(m.title, [m.year, m.quality, size(m.size)].filter(Boolean).join(' · '));
  const busy = ['queued', 'running'].includes(m.job_status);
  $('#sheet-actions').innerHTML = `
    <button type="button" class="btn sm" data-action="clean-movie" ${busy ? 'disabled' : ''}>
      ${isDone(m.job_status) ? 'Censor again' : 'Censor this film'}</button>
    ${m.job_id ? '<button type="button" class="btn secondary sm" data-action="open-job" data-id="' + m.job_id + '">What was muted</button>' : ''}
    ${isDone(m.job_status) ? '<button type="button" class="btn ghost sm" data-action="remove-movie">Remove the censored track</button>' : ''}`;
  const status = {
    done: `Censored ${when(m.cleaned_at)}, ${plural(m.muted || 0, 'word')} muted.`,
    skipped: `Checked ${when(m.cleaned_at)}: nothing to mute.`,
    queued: 'Waiting in the queue.', running: 'Being censored now.', failed: 'The last attempt failed. See the queue for why.',
  }[m.job_status] || 'Not censored yet.';
  $('#sheet-body').innerHTML = `<p>${esc(status)}</p>
    <p class="muted small" style="margin-top:12px">File: <code>${esc(m.path)}</code></p>`;
}

const movieJob = (m, extra = {}) => ({ kind: 'movie', title: m.title, subtitle: String(m.year || ''),
  path: m.path, source: m.source || state.settings?.media_server || '', source_id: String(m.id), ...extra });
ACTIONS['clean-movie'] = async (el) => {
  const m = state.sheet.movie;
  const wanted = await confirmRedo([{ ...m, label: m.title }]);
  if (!wanted.length) return;
  setBusy(el, true);
  await queue([movieJob(m, { force: isDone(m.job_status) })]).finally(() => setBusy(el, false));
  m.job_status = 'queued';
  openMovie(m);
  if (state.view === 'movies') renderMovies();
};
ACTIONS['remove-movie'] = async () => {
  const m = state.sheet.movie;
  await removeTracks([m], m.title, (x) => movieJob(x, { subtitle: `${x.year || ''} (removing)`, action: 'remove' }));
};

/* ---------------------------------------------------------------- a job */

ACTIONS['open-job'] = (el) => openJob(el.dataset.id);

const CATEGORY_NAMES = { custom: 'your own list' };
const categoryName = (key) => state.labels[key] || CATEGORY_NAMES[key] || key;

async function openJob(id) {
  state.sheet = { kind: 'job', id: Number(id), changed: false };
  openSheet('Loading…');
  $('#sheet-body').innerHTML = skeletonRows(4);
  let data;
  try {
    data = await api(`/api/jobs/${id}`);
  } catch (err) {
    $('#sheet-title').textContent = 'Job details';
    $('#sheet-body').innerHTML = problem('Could not load this job', err.message, null, { settings: false });
    return;
  }
  if (!state.settings) await loadSettings({ fill: false }).catch(() => {});
  state.sheet.job = data.job;
  state.sheet.detections = data.detections;
  renderJob();
}

function renderJob() {
  const { job, detections, changed } = state.sheet;
  $('#sheet-title').textContent = `${job.title}`;
  $('#sheet-sub').textContent = job.subtitle || '';
  const busy = ['queued', 'running'].includes(job.status);
  $('#sheet-actions').innerHTML = `${badge(job.status)}
    ${!busy && (job.action || 'clean') === 'clean' ? `<button type="button" class="btn sm" data-action="job-again">Censor again</button>` : ''}
    ${['failed', 'cancelled'].includes(job.status) ? `<button type="button" class="btn secondary sm" data-action="retry" data-id="${job.id}">Retry</button>` : ''}
    ${busy ? `<button type="button" class="btn ghost sm" data-action="cancel-job" data-id="${job.id}">Cancel</button>` : ''}
    ${job.status === 'done' && (job.action || 'clean') === 'clean' ? `<button type="button" class="btn ghost sm" data-action="job-remove">Remove the censored track</button>` : ''}`;

  const left = detections.filter((d) => !d.muted);
  const message = job.status === 'failed'
    ? `<div class="alert error" role="alert">${icon('alert')}<div class="grow"><strong>This job failed</strong>${esc(job.message)}</div></div>`
    : (job.message ? `<p class="muted">${esc(job.message)}</p>` : '');
  const meta = `<div class="job-meta">
    <div><b>${job.muted || 0}</b><span>muted</span></div>
    ${left.length ? `<div><b>${left.length}</b><span>found but left in</span></div>` : ''}
    ${job.added_bytes ? `<div><b>${size(job.added_bytes)}</b><span>censored track</span></div>` : ''}
    ${job.duration ? `<div><b>${stamp(job.duration)}</b><span>long</span></div>` : ''}
    ${job.finished_at ? `<div><b>${when(job.finished_at)}</b><span>finished</span></div>` : ''}
  </div>`;
  // A decision is waiting while it disagrees with what this job did.
  const waiting = detections.filter(pending).length;
  const what = [
    waiting ? `${plural(waiting, 'word')} you corrected` : '',
    changed ? 'word lists changed' : '',
  ].filter(Boolean).join(', and ');
  const banner = what ? `<div class="alert info" role="status">${icon('info')}<div class="grow">
      <strong>${what[0].toUpperCase()}${what.slice(1)}</strong>Censor this file again to apply ${waiting + (changed ? 1 : 0) === 1 ? 'it' : 'them'}.
      Whisper's transcript is kept, so it does not listen again.
      <div class="actions"><button type="button" class="btn sm" data-action="job-again">Censor again now</button></div></div></div>` : '';

  const rows = detections.map((d) => {
    const word = esc(d.text.replace(/^[^\w']+|[^\w']+$/g, '') || d.text);
    // This one moment first: the lists below change every future match.
    const one = d.override
      ? `<button type="button" data-action="override" data-id="${d.id}" data-verdict="clear">${icon('refresh')}Undo my choice for this one</button>`
      : (d.muted
        ? `<button type="button" data-action="override" data-id="${d.id}" data-verdict="keep">${icon('check')}Leave this one in (only at ${stamp(d.start)})</button>`
        : `<button type="button" data-action="override" data-id="${d.id}" data-verdict="mute">${icon('mute')}Mute this one (only at ${stamp(d.start)})</button>`);
    const menu = d.muted
      ? `${one}
         <button type="button" data-action="correct" data-id="${d.id}" data-list="never_here">${icon('tv')}Never mute “${word}” in ${esc(job.title)}</button>
         <button type="button" data-action="correct" data-id="${d.id}" data-list="never">${icon('x')}Never mute “${word}” anywhere</button>
         <button type="button" data-action="correct" data-id="${d.id}" data-list="context">${icon('chat')}Check “${word}” in context</button>
         ${state.settings?.judge_url ? '' : '<p class="note">Checking in context needs a second opinion (Settings), and only applies where the subtitles do not settle the word.</p>'}`
      : `${one}
         <button type="button" data-action="correct" data-id="${d.id}" data-list="always">${icon('mute')}Always mute “${word}”</button>`;
    // The evidence a person needs to decide: what Whisper thought of the word,
    // and what the subtitles say at that moment. Neither changed the muting.
    const evidence = [];
    if (d.confidence !== null && d.confidence !== undefined) {
      const pct = Math.round(d.confidence * 100);
      // Only a flag when nothing else settled it: the subtitles having the
      // word answers the doubt.
      evidence.push(unsure(d) && !d.subtitle_state
        ? `<span class="flag">Whisper was only ${pct}% sure of this word, and there is no subtitle to check it</span>`
        : `Whisper ${pct}% sure`);
    }
    if (d.subtitle_state === 'soundalike') {
      evidence.push(`<span class="flag">Subtitles have a word that sounds like it: “${esc(d.subtitle)}”</span>`);
    } else if (d.subtitle_state === 'subtitle_only') {
      evidence.push(`<span class="flag">Only the subtitles have it: “${esc(d.subtitle)}”</span>`);
    } else if (d.subtitle_state === 'differs') {
      evidence.push(`<span class="flag">Subtitles say: “${esc(d.subtitle)}”</span>`);
    } else if (d.subtitle_state === 'replaced') {
      evidence.push(`Subtitles soften it: “${esc(d.subtitle)}”`);
    } else if (d.subtitle_state === 'agrees') {
      evidence.push('Subtitles agree');
    } else if (d.subtitle_state === 'omits') {
      evidence.push('Subtitles leave it out');
    }
    const check = worthChecking(d) && !d.override;
    const choice = d.override === 'keep'
      ? (pending(d) ? 'You said leave this one in. Censor again to apply it.' : 'Left in, as you said.')
      : d.override === 'mute'
        ? (pending(d) ? 'You said mute this one. Censor again to apply it.' : 'Muted, as you said.')
        : '';
    return `<div class="detection${d.fixed || d.override ? ' fixed' : ''}${check ? ' check' : ''}">
      <span class="at">${stamp(d.start)}</span>
      <div><div class="word">${esc(d.text)} ${d.muted ? '<span class="badge done">Muted</span>' : '<span class="badge">Left in</span>'}${
          check ? ' <span class="badge queued">Worth a listen</span>' : ''}</div>
        <div class="reason">${esc([categoryName(d.category), d.reason].filter(Boolean).join(' · '))}</div>
        ${evidence.length ? `<div class="reason">${evidence.join(' · ')}</div>` : ''}
        ${d.fixed ? `<div class="reason" style="color:var(--accent-text)">${esc(d.fixed)}</div>` : ''}
        ${choice ? `<div class="reason" style="color:var(--accent-text)">${esc(choice)}</div>` : ''}</div>
      <details class="menu">
        <summary class="btn ghost sm" aria-label="That was wrong: ${word} at ${stamp(d.start)}">${icon('flag')}That was wrong</summary>
        <div class="menu-list">${menu}</div>
      </details></div>`;
  }).join('');

  const toCheck = detections.filter((d) => worthChecking(d) && !d.override).length;
  const review = toCheck ? `<div class="alert warn">${icon('alert')}<div class="grow">
      <strong>${plural(toCheck, 'word')} worth a listen</strong>
      Whisper was unsure of ${toCheck === 1 ? 'it' : 'them'}, or the subtitles say something else there.
      ${toCheck === 1 ? 'It is' : 'They are'} still muted. If one is wrong, choose <strong>That was wrong → Leave this one in</strong>, then censor again once you have been through them.</div></div>` : '';
  $('#sheet-body').innerHTML = `${message}${banner}${review}${meta}
    <h3 style="font-size:1rem">What was found</h3>
    <p class="muted small">Every word matched, where it was, and why. If one is wrong, say so: for that moment only, or for the word from now on.</p>
    ${detections.length ? `<div class="detections">${rows}</div>`
      : empty({ icon: 'check', title: job.status === 'failed' ? 'Nothing recorded' : 'Nothing found',
        text: job.status === 'failed' ? 'The job stopped before listening finished.' : 'No listed word was heard in this file.' })}`;
}

/* A decision for one moment that this job did not follow yet. */
const pending = (d) => (d.override === 'keep' && !!d.muted) || (d.override === 'mute' && !d.muted);

ACTIONS.override = async (el) => {
  const det = state.sheet.detections.find((d) => String(d.id) === el.dataset.id);
  const result = await api(`/api/detections/${el.dataset.id}/override`,
    { method: 'POST', body: { verdict: el.dataset.verdict } });
  if (det) det.override = result.verdict;
  renderJob();
  const at = stamp(result.at);
  toast(result.verdict === 'keep' ? `“${result.word}” at ${at} will be left in next time`
    : result.verdict === 'mute' ? `“${result.word}” at ${at} will be muted next time`
      : `Your choice for “${result.word}” at ${at} is gone`);
};

const LIST_NAMES = { never: 'Never silence', never_here: 'this show’s exceptions',
  context: 'Check in context', always: 'Always silence' };

ACTIONS.correct = async (el) => {
  const det = state.sheet.detections.find((d) => String(d.id) === el.dataset.id);
  const list = el.dataset.list;
  const result = await api(`/api/detections/${el.dataset.id}/correct`, { method: 'POST', body: { list } });
  state.settings = null;          // the lists changed; reload before the next save
  const note = {
    never: `“${result.word}” will never be muted`,
    never_here: `“${result.word}” will never be muted in ${result.title}`,
    context: result.judge_configured
      ? `“${result.word}” will be checked in context where the subtitles do not settle it`
      : `“${result.word}” is on the check list, but no second opinion is set up, so it is still muted. To fix one moment, use “Leave this one in”`,
    always: `“${result.word}” will always be muted`,
  }[list];
  state.sheet.detections.forEach((d) => {
    const same = d.text.replace(/^[^\w']+|[^\w']+$/g, '').toLowerCase() === result.word;
    if (same) d.fixed = `On ${LIST_NAMES[list]} from now on.`;
  });
  if (det) det.fixed = `On ${LIST_NAMES[list]} from now on.`;
  const scope = list === 'never_here' ? `?title=${encodeURIComponent(result.title)}` : '';
  state.sheet.changed = true;
  renderJob();
  const undo = list === 'always' ? null : {
    label: 'Undo',
    run: async () => {
      await api(`/api/words/${list}/${encodeURIComponent(result.word)}${scope}`, { method: 'DELETE' });
      state.sheet?.detections?.forEach((d) => { if (d.fixed) delete d.fixed; });
      if (state.sheet?.kind === 'job') { state.sheet.changed = false; renderJob(); }
      toast(`Took “${result.word}” back off ${LIST_NAMES[list]}`);
    },
  };
  toast(note, undo ? { action: undo } : {});
};

ACTIONS['job-again'] = async () => {
  const job = state.sheet.job;
  await queue([{ kind: job.kind, title: job.title, subtitle: job.subtitle, path: job.path,
    source: job.source, source_id: job.source_id, force: true }]);
  closeSheet();
};
ACTIONS['job-remove'] = async () => {
  const job = state.sheet.job;
  await removeTracks([{ ...job, job_status: job.status }], `${job.title} ${job.subtitle || ''}`.trim(),
    (x) => ({ kind: x.kind, title: x.title, subtitle: `${x.subtitle || ''} (removing)`, path: x.path,
      source: x.source, source_id: x.source_id, action: 'remove' }));
  closeSheet();
};

/* ======================================================================
   Upcoming
   ====================================================================== */

const dayHeading = (iso) => {
  const day = new Date(`${iso}T00:00:00`);
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const days = Math.round((day - midnight) / 86400000);
  const name = day.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'short' });
  if (days < 0) return `Yesterday · ${name}`;
  if (days === 0) return `Today · ${name}`;
  if (days === 1) return `Tomorrow · ${name}`;
  return name;
};
const airTime = (iso) => (iso ? new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : '');
const shortDate = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '');

async function loadCalendar() {
  if (!state.calendar) $('#calendar-list').innerHTML = loading('Loading the calendar…', skeletonRows(5));
  try {
    const data = await api(`/api/calendar?days=${$('#calendar-days').value}`);
    if (data.unavailable) {
      state.calendar = [];
      $('#calendar-list').innerHTML = empty({ icon: 'calendar', title: 'Nothing here knows what is coming',
        text: `${esc(sourceName('show'))} only knows what it already has. Use Sonarr as the library, or add Radarr, to see what is coming.`,
        actions: '<a class="btn secondary sm" href="#/settings/library">Open settings</a>' });
      return;
    }
    // Episodes and film releases in one list; a film is told apart by kind.
    state.calendar = [...data.items, ...(data.films || []).map((f) => ({ ...f, kind: 'film' }))];
    state.calendarProblems = data.problems || [];
  } catch (err) {
    if (err.status === 401) return;
    $('#calendar-list').innerHTML = problem('Could not load the calendar', err.message, 'reload-calendar');
    return;
  }
  renderCalendar();
}
ACTIONS['reload-calendar'] = () => { state.calendar = null; return loadCalendar(); };

function renderCalendar() {
  if (!state.calendar) return;
  const filter = $('#calendar-filter').value;
  const film = (e) => e.kind === 'film';
  const set = (e) => (film(e) ? e.marked : e.monitored);
  const items = state.calendar.filter((e) => {
    if (filter === 'missing') return !e.has_file;
    if (filter === 'monitored') return set(e);
    if (filter === 'unmonitored') return !set(e);
    if (filter === 'episodes') return !film(e);
    if (filter === 'films') return film(e);
    return true;
  }).sort((a, b) => String(a.airs || '').slice(0, 10).localeCompare(String(b.airs || '').slice(0, 10))
    || Number(film(a)) - Number(film(b)));
  const films = items.filter(film).length;
  $('#calendar-count').textContent = [items.length - films ? plural(items.length - films, 'episode') : '',
    films ? plural(films, 'film') : ''].filter(Boolean).join(' · ') || 'Nothing';
  const note = (state.calendarProblems || []).length
    ? `<div class="alert warn">${icon('alert')}<div class="grow"><strong>Not everything answered</strong>${
      state.calendarProblems.map((p) => `<div>${esc(p)}</div>`).join('')}</div></div>` : '';
  const days = [];
  const byDay = new Map();
  items.forEach((e) => {
    const key = (e.airs || '').slice(0, 10);
    if (!byDay.has(key)) { byDay.set(key, []); days.push(key); }
    byDay.get(key).push(e);
  });
  const filmRow = (f) => `
      <div class="row">
        <img class="thumb" loading="lazy" decoding="async" alt="" src="${posterUrl(f, 'movie')}" onerror="this.classList.add('none')">
        <div class="grow">
          <div class="title">${esc(f.title)} <span class="muted">${esc([f.year, 'film'].filter(Boolean).join(' · '))}</span></div>
          <div class="sub">Digital release</div>
          ${f.has_file ? '<div class="sub good">Downloaded</div>' : ''}
        </div>
        <div class="actions">
          ${f.has_file ? '' : `<label class="switch"><input type="checkbox" role="switch" data-change="want-film" data-id="${esc(f.id)}"
            data-title="${esc(f.title)}" ${f.marked ? 'checked' : ''}>
            <span class="track" aria-hidden="true"></span><span>Censor when it downloads<span class="sr-only">: ${esc(f.title)}</span></span></label>`}
        </div>
      </div>`;
  $('#calendar-list').innerHTML = note + (days.length ? days.map((key) => `
    <h2 class="day-head">${esc(dayHeading(key))}</h2>
    <div class="rows">${byDay.get(key).map((e) => (film(e) ? filmRow(e) : `
      <div class="row">
        <img class="thumb" loading="lazy" decoding="async" alt="" src="${posterUrl(e, 'episode')}" onerror="this.classList.add('none')">
        <div class="grow">
          <div class="title">${esc(e.series)} <span class="muted">${epCode(e.season, e.episode)}${e.title ? ` · ${esc(e.title)}` : ''}</span></div>
          <div class="sub">${esc([`airs ${airTime(e.airs)}`, e.network, e.runtime ? `${e.runtime} min` : ''].filter(Boolean).join(' · '))}</div>
          ${e.has_file ? `<div class="sub good">${e.downloaded_at
            ? `Downloaded ${esc(shortDate(e.downloaded_at))} at ${esc(airTime(e.downloaded_at))}` : 'Downloaded'}</div>` : ''}
        </div>
        <div class="actions">
          ${e.has_file ? '' : (e.wanted ? '<span class="badge queued">Wanted</span>' : '<span class="badge">Not wanted</span>')}
          <label class="switch"><input type="checkbox" role="switch" data-change="watch" data-id="${esc(e.series_id)}"
            data-title="${esc(e.series)}" ${e.monitored ? 'checked' : ''}>
            <span class="track" aria-hidden="true"></span><span>Censor new episodes<span class="sr-only"> of ${esc(e.series)}</span></span></label>
          <button type="button" class="btn ghost sm" data-action="open-show" data-id="${esc(e.series_id)}" data-title="${esc(e.series)}">Open</button>
        </div>
      </div>`)).join('')}</div>`).join('')
    : empty({ icon: 'calendar', title: 'Nothing coming', text: 'Nothing in that window matches.' }));
}
$('#calendar-days').addEventListener('change', () => { state.calendar = null; loadCalendar(); });
$('#calendar-filter').addEventListener('change', renderCalendar);

/* ======================================================================
   Queue
   ====================================================================== */

function renderQueue() {
  const data = state.jobs;
  if (!data) {
    $('#job-list').innerHTML = loading('Loading the queue…', skeletonRows(4));
    return;
  }
  const items = data.items;
  const queued = items.filter((j) => j.status === 'queued');
  const failed = items.filter((j) => j.status === 'failed');
  const cancelled = items.filter((j) => j.status === 'cancelled');
  state.queueIds = queued.map((j) => j.id);
  state.picked = new Set([...state.picked].filter((id) => state.queueIds.includes(id)));

  morph($('#queue-now'), nowCard({ withCancel: true }));
  $('#clear-queue').hidden = !queued.length;
  const ago = data.last_check ? Math.max(0, Math.round((Date.now() / 1000 - data.last_check) / 60)) : null;
  const watching = [
    data.monitors ? `${plural(data.monitors, 'show')} censoring new episodes` : '',
    data.films_auto ? 'new films censored as they arrive' : '',
    data.films_waiting ? `${plural(data.films_waiting, 'film')} to censor when ${
      data.films_waiting === 1 ? 'it downloads' : 'they download'}` : '',
  ].filter(Boolean).join(', ');
  $('#monitor-status').textContent = watching
    ? `${watching[0].toUpperCase()}${watching.slice(1)}. Checked every 10 minutes${
      ago === null ? '' : `, last ${ago === 0 ? 'just now' : `${ago} min ago`}`}.${
      data.films_note ? ` ${data.films_note[0].toUpperCase()}${data.films_note.slice(1)}.` : ''}`
    : '';
  renderSelection();

  const queuedRow = (j, i) => {
    const picked = state.picked.has(j.id);
    const name = `${j.title} ${j.subtitle || ''}`.trim();
    return `<div class="row${picked ? ' picked' : ''}">
      <input type="checkbox" class="pick" data-action="pick" data-id="${j.id}" data-key="pick-${j.id}"
        aria-label="Select ${esc(name)}" ${picked ? 'checked' : ''}>
      <div class="grow">
        <div class="title"><button type="button" class="linkish" data-action="pick-show" data-title="${esc(j.title)}"
          data-key="show-${j.id}" title="Select every queued episode of this show">${esc(j.title)}</button>
          <span class="muted">${esc(j.subtitle || '')}</span></div>
        <div class="sub">${i === 0 && !runningJob() ? 'Next up' : `#${i + 1} in line`}${
          j.action === 'remove' ? ' · removing the censored track' : ''}${j.force ? ' · censoring again' : ''}${
          j.message && j.message !== 'retrying' ? ` · ${esc(j.message)}` : ''}</div>
      </div>
      <div class="actions">
        <div class="btn-group">
          <button type="button" class="icon-btn" data-action="move" data-id="${j.id}" data-where="top" data-key="top-${j.id}" aria-label="Run ${esc(name)} first" title="First" ${i === 0 ? 'disabled' : ''}>${icon('top')}</button>
          <button type="button" class="icon-btn" data-action="move" data-id="${j.id}" data-where="up" data-key="up-${j.id}" aria-label="Move ${esc(name)} up" title="Up" ${i === 0 ? 'disabled' : ''}>${icon('up')}</button>
          <button type="button" class="icon-btn" data-action="move" data-id="${j.id}" data-where="down" data-key="down-${j.id}" aria-label="Move ${esc(name)} down" title="Down" ${i === queued.length - 1 ? 'disabled' : ''}>${icon('down')}</button>
          <button type="button" class="icon-btn" data-action="move" data-id="${j.id}" data-where="bottom" data-key="bottom-${j.id}" aria-label="Run ${esc(name)} last" title="Last" ${i === queued.length - 1 ? 'disabled' : ''}>${icon('bottom')}</button>
        </div>
        <button type="button" class="btn ghost sm" data-action="cancel-job" data-id="${j.id}" data-key="cancel-${j.id}" aria-label="Cancel ${esc(name)}">Cancel</button>
      </div></div>`;
  };
  const endedRow = (j) => {
    const name = `${j.title} ${j.subtitle || ''}`.trim();
    return `<div class="row">
      <div class="grow">
        <div class="title">${esc(j.title)} <span class="muted">${esc(j.subtitle || '')}</span></div>
        <div class="sub"${j.status === 'failed' ? ' style="color:var(--error)"' : ''}>${esc(j.message || '')}</div>
      </div>
      <div class="actions">${badge(j.status)}
        <button type="button" class="btn ghost sm" data-action="open-job" data-id="${j.id}" data-key="details-${j.id}" aria-label="Details for ${esc(name)}">Details</button>
        <button type="button" class="btn secondary sm" data-action="retry" data-id="${j.id}" data-key="retry-${j.id}" aria-label="Retry ${esc(name)}">Retry</button>
      </div></div>`;
  };

  let html = '';
  if (queued.length) {
    html += `<div class="group-head"><h2>Up next · ${queued.length}</h2></div><div class="rows">${queued.map(queuedRow).join('')}</div>`;
  }
  if (failed.length) {
    html += `<div class="group-head"><h2>Failed · ${failed.length}</h2>
      <button type="button" class="btn secondary sm" data-action="retry-all" data-key="retry-all">Retry all</button>
      <button type="button" class="btn ghost sm" data-action="purge" data-what="failed" data-key="purge-failed">Clear</button></div>
      <div class="rows">${failed.map(endedRow).join('')}</div>`;
  }
  if (cancelled.length) {
    html += `<div class="group-head"><h2>Cancelled · ${cancelled.length}</h2>
      <button type="button" class="btn ghost sm" data-action="purge" data-what="cancelled" data-key="purge-cancelled">Clear</button></div>
      <div class="rows">${cancelled.map(endedRow).join('')}</div>`;
  }
  if (!html && !runningJob()) {
    html = empty({ icon: 'queue', title: 'Nothing waiting',
      text: 'Pick a show or a film to censor. Shows set to censor new episodes add them here as they arrive.',
      actions: `<a class="btn sm" href="#/shows">Browse shows</a><a class="btn secondary sm" href="#/movies">Browse films</a>` });
  }
  morph($('#job-list'), html);
}

function renderSelection() {
  const n = state.picked.size;
  $('#selection-bar').hidden = n === 0;
  $('#selection-count').textContent = n ? `${n} selected of ${state.queueIds.length}` : '';
}

ACTIONS.pick = (el, event) => {
  const id = Number(el.dataset.id);
  if (event.shiftKey && state.lastPicked !== null) {
    const from = state.queueIds.indexOf(state.lastPicked);
    const to = state.queueIds.indexOf(id);
    if (from !== -1 && to !== -1) {
      const [lo, hi] = from < to ? [from, to] : [to, from];
      state.queueIds.slice(lo, hi + 1).forEach((x) => state.picked.add(x));
    }
  } else if (el.checked) state.picked.add(id);
  else state.picked.delete(id);
  state.lastPicked = id;
  renderQueue();
};
ACTIONS['pick-show'] = (el) => {
  const ids = (state.jobs.items || []).filter((j) => j.status === 'queued' && j.title === el.dataset.title).map((j) => j.id);
  const allOn = ids.every((id) => state.picked.has(id));
  ids.forEach((id) => (allOn ? state.picked.delete(id) : state.picked.add(id)));
  renderQueue();
};
ACTIONS.move = async (el) => {
  await api(`/api/jobs/${el.dataset.id}/move`, { method: 'POST', body: { where: el.dataset.where } });
  await poll();
};
ACTIONS['cancel-job'] = async (el) => {
  await api(`/api/jobs/${el.dataset.id}`, { method: 'DELETE' });
  toast('Cancelled');
  await poll();
  if (state.sheet?.kind === 'job') openJob(state.sheet.id);
};
ACTIONS.retry = async (el) => {
  await api('/api/jobs/retry', { method: 'POST', body: { ids: [Number(el.dataset.id)] } });
  toast('Back in the queue');
  await poll();
  if (state.sheet?.kind === 'job') openJob(state.sheet.id);
};
ACTIONS['retry-all'] = async () => {
  const { retrying } = await api('/api/jobs/retry', { method: 'POST', body: {} });
  toast(`${plural(retrying, 'job')} back in the queue`);
  poll();
};
ACTIONS.purge = async (el) => {
  const what = el.dataset.what;
  if (what === 'failed') {
    const answer = await ask('Clear the failed jobs?',
      'They leave the list. The files were never changed: a job only touches a file after it verifies.',
      [{ label: 'Clear them', value: 'yes', primary: true }]);
    if (answer !== 'yes') return;
  }
  const { removed } = await api(`/api/jobs/${what}`, { method: 'DELETE' });
  toast(`Cleared ${plural(removed, 'job')}`);
  poll();
};
$('#selection-bar').addEventListener('click', async (event) => {
  const el = event.target.closest('[data-bulk]');
  if (!el) return;
  const ids = [...state.picked];
  try {
    if (el.dataset.bulk === 'clear') {
      state.picked.clear(); state.lastPicked = null;
      renderQueue();
      $('#job-list .pick')?.focus();
      return;
    } else if (el.dataset.bulk === 'cancel') {
      const answer = await ask(`Cancel ${plural(ids.length, 'job')}?`,
        'They come out of the queue. Nothing already censored is affected.',
        [{ label: 'Cancel them', value: 'yes', primary: true }]);
      if (answer !== 'yes') return;
      const { cancelled } = await api('/api/jobs/cancel', { method: 'POST', body: { ids } });
      toast(`Cancelled ${cancelled}`);
      state.picked.clear();
    } else {
      await api('/api/jobs/move', { method: 'POST', body: { ids, where: el.dataset.bulk } });
    }
  } catch (err) { fail(err); }
  await poll();
  renderQueue();
});
$('#check-new').addEventListener('click', async (event) => {
  const btn = event.currentTarget;
  setBusy(btn, true);
  try {
    const { queued, films = 0 } = await api('/api/monitors/check', { method: 'POST' });
    const found = [
      queued - films ? plural(queued - films, 'new episode') : '',
      films ? plural(films, 'new film') : '',
    ].filter(Boolean).join(' and ');
    toast(found ? `Queued ${found}` : 'Nothing new to censor');
    poll();
  } catch (err) { fail(err); } finally { setBusy(btn, false); }
});
$('#clear-queue').addEventListener('click', async () => {
  const answer = await ask('Empty the queue?',
    'Everything waiting is cancelled. A file being censored right now finishes.',
    [{ label: 'Empty it', value: 'yes', primary: true }], { danger: true });
  if (answer !== 'yes') return;
  try {
    const { cancelled } = await api('/api/queue', { method: 'DELETE' });
    toast(`Cancelled ${cancelled}`);
    poll();
  } catch (err) { fail(err); }
});

/* ======================================================================
   Censored
   ====================================================================== */

async function loadCleaned() {
  const query = $('#cleaned-search').value.trim();
  if (!state.history) $('#cleaned-list').innerHTML = loading('Loading…', skeletonRows(5));
  let data;
  try {
    data = await api(`/api/history?q=${encodeURIComponent(query)}`);
  } catch (err) {
    if (err.status === 401) return;
    $('#cleaned-list').innerHTML = problem('Could not load what has been censored', err.message, 'reload-cleaned', { settings: false });
    return;
  }
  const onlyCheck = $('#cleaned-filter').value === 'check';
  state.history = onlyCheck ? data.items.filter((j) => j.to_check) : data.items;
  const { stats } = data;
  const unknown = data.items.filter((j) => !j.added_bytes && j.status === 'done').length;
  $('#cleaned-stats').innerHTML = `
    <div class="stat"><b>${stats.cleaned_files}</b><span>${stats.cleaned_files === 1 ? 'file' : 'files'} with a censored track</span></div>
    <div class="stat"><b>${stats.words_muted}</b><span>words muted</span></div>
    <div class="stat"><b>${size(stats.added_bytes) || '0 MB'}</b><span>of censored audio${unknown ? ' (some older files not counted)' : ''}</span></div>`;
  const items = state.history;
  $('#cleaned-count-note').textContent = query || onlyCheck ? plural(items.length, 'match', 'matches') : '';
  $('#remove-all-zone').hidden = !stats.cleaned_files;
  $('#cleaned-list').innerHTML = items.length
    ? `<div class="rows">${items.map((j, i) => `
      <div class="row">
        <div class="grow">
          <div class="title">${esc(j.title)} <span class="muted">${esc(j.subtitle || '')}</span></div>
          <div class="sub">${esc([when(j.finished_at), j.muted ? plural(j.muted, 'word') + ' muted' : 'nothing to mute',
            size(j.added_bytes)].filter(Boolean).join(' · '))}</div>
        </div>
        <div class="actions">
          ${j.to_check ? `<span class="badge queued">${j.to_check} worth a listen</span>` : ''}
          <button type="button" class="btn ghost sm" data-action="open-job" data-id="${j.id}" aria-label="Details for ${esc(j.title)} ${esc(j.subtitle || '')}">Details</button>
          ${j.status === 'done' ? `<button type="button" class="btn ghost sm" data-action="remove-history" data-index="${i}"
            aria-label="Remove the censored track from ${esc(j.title)} ${esc(j.subtitle || '')}">Remove</button>` : ''}
        </div></div>`).join('')}</div>`
    : (onlyCheck && !query ? empty({ icon: 'check', title: 'Nothing to check',
        text: 'No censored file has a word Whisper was unsure of, or one its subtitles disagree with.' })
      : query ? empty({ icon: 'search', title: 'Nothing matches', text: `No censored file matches “${esc(query)}”.` })
      : empty({ icon: 'done', title: 'Nothing censored yet',
        text: 'Files you censor show up here, with what was muted and what the extra track costs.',
        actions: '<a class="btn sm" href="#/shows">Pick a show</a>' }));
}
ACTIONS['reload-cleaned'] = loadCleaned;
$('#cleaned-search').addEventListener('input', debounce(loadCleaned, 250));
$('#cleaned-filter').addEventListener('change', loadCleaned);

ACTIONS['remove-history'] = async (el) => {
  const j = state.history[Number(el.dataset.index)];
  await removeTracks([{ ...j, job_status: j.status }], `${j.title} ${j.subtitle || ''}`.trim(),
    (x) => ({ kind: x.kind, title: x.title, subtitle: `${x.subtitle || ''} (removing)`, path: x.path,
      source: x.source, source_id: x.source_id, action: 'remove' }));
  loadCleaned();
};

$('#remove-all').addEventListener('click', async () => {
  try {
    const { stats } = await api('/api/history?limit=1');
    const first = await ask(`Remove the censored track from all ${stats.cleaned_files} files?`,
      `Every “${trackName()}” track goes, giving back ${size(stats.added_bytes) || 'its space'}. The original audio is untouched. Putting them back means censoring everything again.`,
      [{ label: 'Continue', value: 'go', primary: true }], { danger: true });
    if (first !== 'go') return;
    const second = await ask('Are you sure?', `This queues ${plural(stats.cleaned_files, 'removal')}.`,
      [{ label: `Remove all ${stats.cleaned_files}`, value: 'yes', primary: true }], { danger: true });
    if (second !== 'yes') return;
    const { queued } = await api('/api/history/remove-all', { method: 'POST', body: { confirm: 'REMOVE ALL' } });
    toast(`Queued ${plural(queued, 'removal')}`);
    poll(); loadCleaned();
  } catch (err) { fail(err); }
});

/* ======================================================================
   Settings
   ====================================================================== */

const form = $('#settings-form');
const F = (name) => form.elements[name];
const radio = (name) => ($$(`input[name="${name}"]`).find((r) => r.checked) || {}).value;
let snapshot = '';

/* A word list as chips, with a box to add more. Enter or a comma adds;
   pasting a list adds each line. */
function tagInput(host, words) {
  host._words = [...words];
  const label = $(`#${host.getAttribute('aria-labelledby')}`)?.textContent || 'this list';
  const draw = () => {
    host.innerHTML = host._words.map((w, i) => `<span class="tag">${esc(w)}<button type="button"
      data-remove="${i}" aria-label="Remove ${esc(w)}">${icon('x')}</button></span>`).join('')
      + `<input type="text" aria-label="Add a word to ${esc(label)}" placeholder="${host._words.length ? 'Add…' : 'Type a word and press Enter'}" autocomplete="off" autocapitalize="none">`;
  };
  const add = (text) => {
    const fresh = text.split(/[\n,]/).map((w) => w.trim()).filter(Boolean);
    let changed = false;
    fresh.forEach((w) => {
      if (!host._words.some((x) => x.toLowerCase() === w.toLowerCase())) { host._words.push(w); changed = true; }
    });
    return changed;
  };
  host.onkeydown = (e) => {
    const input = e.target.closest('input');
    if (!input) return;
    if ((e.key === 'Enter' || e.key === ',') && input.value.trim()) {
      e.preventDefault();
      add(input.value); draw(); host.querySelector('input').focus(); markDirty();
    } else if (e.key === 'Backspace' && !input.value && host._words.length) {
      host._words.pop(); draw(); host.querySelector('input').focus(); markDirty();
    }
  };
  host.onpaste = (e) => {
    const text = e.clipboardData?.getData('text') || '';
    if (/[\n,]/.test(text)) { e.preventDefault(); add(text); draw(); host.querySelector('input').focus(); markDirty(); }
  };
  host.onfocusout = (e) => {
    const input = e.target.closest('input');
    if (input && input.value.trim() && !host.contains(e.relatedTarget)) {
      if (add(input.value)) { draw(); markDirty(); } else input.value = '';
    }
  };
  host.onclick = (e) => {
    const btn = e.target.closest('[data-remove]');
    if (btn) {
      host._words.splice(Number(btn.dataset.remove), 1);
      draw(); host.querySelector('input').focus(); markDirty();
    } else if (e.target === host) host.querySelector('input').focus();
  };
  draw();
}
const tags = (name) => tagWords($(`[data-tags="${name}"]`));
const tagWords = (host) => {
  const pending = host.querySelector('input')?.value.trim();
  const words = [...(host._words || [])];
  if (pending && !words.some((w) => w.toLowerCase() === pending.toLowerCase())) words.push(pending);
  return words;
};

function collect() {
  return {
    library_source: radio('library_source') || 'sonarr',
    sonarr: { url: F('sonarr.url').value.trim(), api_key: F('sonarr.api_key').value.trim(), enabled: true },
    radarr: { url: F('radarr.url').value.trim(), api_key: F('radarr.api_key').value.trim(), enabled: true },
    plex_url: F('plex_url').value.trim(), plex_token: F('plex_token').value.trim(),
    jellyfin_url: F('jellyfin_url').value.trim(), jellyfin_api_key: F('jellyfin_api_key').value.trim(),
    categories: $$('#categories input:checked').map((i) => i.value),
    custom_words: tags('custom_words'),
    allow_words: tags('allow_words'),
    asr_backend: radio('asr_backend') || 'builtin',
    asr_url: F('asr_url').value.trim(), asr_api_key: F('asr_api_key').value.trim(),
    asr_remote_model: F('asr_remote_model').value.trim(),
    device: F('device').value,
    trim_silence: F('trim_silence').checked,
    recheck_url: F('recheck_url').value.trim(), recheck_model: F('recheck_model').value.trim(),
    recheck_api_key: F('recheck_api_key').value.trim(),
    pad_start: F('pad_start').value, pad_end: F('pad_end').value, fade: F('fade').value,
    track_title: F('track_title').value.trim(),
    check_in_context: tags('check_in_context'),
    allow_words_by_title: Object.fromEntries($$('[data-title-tags]')
      .map((host) => [host.dataset.titleTags, tagWords(host)])
      .filter(([, list]) => list.length)),
    judge_url: F('judge_url').value.trim(), judge_model: F('judge_model').value.trim(),
    judge_threads: Number(F('judge_threads').value),
    media_server: radio('media_server') || 'none',
    hold_policy: F('hold_policy').value,
    bitrate_surround: F('bitrate_surround').value.trim(), bitrate_stereo: F('bitrate_stereo').value.trim(),
    ffmpeg_threads: F('ffmpeg_threads').value, compute_type: F('compute_type').value,
    judge_keep_alive: F('judge_keep_alive').value.trim(),
    keep_backup: F('keep_backup').checked,
  };
}

function markDirty() {
  if (!snapshot) return;
  settingsDirty = JSON.stringify(collect()) !== snapshot;
  const bar = $('#save-bar');
  bar.hidden = !settingsDirty;
  if (settingsDirty && !bar.classList.contains('error')) $('#save-state').textContent = 'Unsaved changes';
  renderContextNote();
}
form.addEventListener('input', markDirty);
form.addEventListener('change', (e) => {
  if (e.target.name === 'library_source') renderLibrarySource();
  if (e.target.name === 'media_server') { renderMediaServer(); mediaNow(); }
  if (e.target.name === 'asr_backend') renderAsrBackend();
  if (e.target.name === 'device') renderHardwareNote();
  if (e.target.getAttribute('aria-invalid')) clearError(e.target.name);
  markDirty();
});
form.addEventListener('input', (e) => { if (e.target.getAttribute('aria-invalid')) clearError(e.target.name); });
window.addEventListener('beforeunload', (e) => { if (settingsDirty) { e.preventDefault(); e.returnValue = ''; } });

async function loadSettings({ fill = true, force = false } = {}) {
  if (state.settings && !force && form._filled) return state.settings;
  if (fill && !form._filled) {
    $('#settings-loading').innerHTML = loading('Loading settings…', skeletonRows(3));
  }
  let settings;
  try {
    settings = await api('/api/settings');
  } catch (err) {
    if (fill && err.status !== 401) {
      $('#settings-loading').innerHTML = problem('Could not load settings', err.message, 'reload-settings', { settings: false });
    }
    throw err;
  }
  state.settings = settings;
  settings.available_categories.forEach((c) => { state.labels[c.key] = c.label; });
  noteSource(settings);
  window.__auth = { configured: !!settings.auth_enabled, username: settings.auth_user || '' };
  $('#more-signout').hidden = !settings.auth_enabled;
  if (fill) fillSettings(settings);
  return settings;
}
ACTIONS['reload-settings'] = () => loadSettings({ force: true });

function fillSettings(s) {
  $('#settings-loading').innerHTML = '';
  form.hidden = false;
  $('#s-security').hidden = false;
  const set = (name, value) => { if (F(name)) F(name).value = value ?? ''; };
  const check = (name, value) => $$(`input[name="${name}"]`).forEach((r) => { r.checked = r.value === value; });
  check('library_source', s.library_source === 'arr' ? 'sonarr' : (s.library_source || 'sonarr'));
  check('asr_backend', s.asr_backend || 'builtin');
  check('media_server', s.media_server || 'none');
  set('sonarr.url', s.sonarr.url); set('sonarr.api_key', s.sonarr.api_key);
  set('radarr.url', s.radarr?.url); set('radarr.api_key', s.radarr?.api_key);
  set('plex_url', s.plex_url); set('plex_token', s.plex_token);
  set('jellyfin_url', s.jellyfin_url); set('jellyfin_api_key', s.jellyfin_api_key);
  set('asr_url', s.asr_url); set('asr_api_key', s.asr_api_key); set('asr_remote_model', s.asr_remote_model);
  set('device', s.device || 'auto');
  F('trim_silence').checked = !!s.trim_silence;
  set('recheck_url', s.recheck_url); set('recheck_model', s.recheck_model);
  set('recheck_api_key', s.recheck_api_key);
  set('pad_start', s.pad_start); set('pad_end', s.pad_end); set('fade', s.fade);
  set('track_title', s.track_title);
  set('judge_url', s.judge_url); set('judge_model', s.judge_model);
  set('judge_threads', String(s.judge_threads));
  if (F('judge_threads').value !== String(s.judge_threads)) {
    F('judge_threads').insertAdjacentHTML('beforeend', `<option value="${Number(s.judge_threads)}">${Number(s.judge_threads)}</option>`);
    set('judge_threads', String(s.judge_threads));
  }
  set('bitrate_surround', s.bitrate_surround); set('bitrate_stereo', s.bitrate_stereo);
  set('ffmpeg_threads', s.ffmpeg_threads); set('compute_type', s.compute_type || 'auto');
  set('judge_keep_alive', s.judge_keep_alive);
  F('keep_backup').checked = !!s.keep_backup;

  $('#categories').innerHTML = '<legend class="sr-only">Word lists</legend>' + s.available_categories.map((c) => `
    <label class="toggle"><span class="grow">${esc(c.label)}<span>${c.count} words and phrases</span></span>
      <span class="switch"><input type="checkbox" role="switch" value="${esc(c.key)}" ${s.categories.includes(c.key) ? 'checked' : ''}>
      <span class="track" aria-hidden="true"></span></span></label>`).join('');
  tagInput($('[data-tags="custom_words"]'), s.custom_words || []);
  tagInput($('[data-tags="allow_words"]'), s.allow_words || []);
  tagInput($('[data-tags="check_in_context"]'), s.check_in_context || []);
  renderTitleExceptions(s.allow_words_by_title || {});

  renderLibrarySource();
  renderMediaServer();
  set('hold_policy', s.hold_policy);
  renderAsrBackend();
  clearErrors();
  form._filled = true;
  snapshot = JSON.stringify(collect());
  settingsDirty = false;
  $('#save-bar').hidden = true;
  $('#save-bar').classList.remove('error');
  renderContextNote();
  renderSecurity();
  refreshPathReport();
  loadModels();
  loadHardware();
  mediaNow();
  loadSetup();
}

function renderTitleExceptions(byTitle) {
  const titles = Object.keys(byTitle).sort((a, b) => a.localeCompare(b));
  const host = $('#title-exceptions');
  host.innerHTML = titles.length ? titles.map((title, i) => `
    <div class="title-exception">
      <div class="title-exception-head"><strong id="l-title-${i}">${esc(title)}</strong>
        <button type="button" class="btn ghost sm" data-drop-title="${i}"
          aria-label="Remove every exception for ${esc(title)}">Remove all</button></div>
      <div class="tags" data-title-tags="${esc(title)}" aria-labelledby="l-title-${i}"></div>
    </div>`).join('')
    : '<p class="hint">None yet.</p>';
  $$('[data-title-tags]', host).forEach((el, i) => tagInput(el, byTitle[titles[i]]));
}
$('#title-exceptions').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-drop-title]');
  if (!btn) return;
  btn.closest('.title-exception').remove();
  if (!$('#title-exceptions .title-exception')) $('#title-exceptions').innerHTML = '<p class="hint">None yet.</p>';
  markDirty();
});

function renderContextNote() {
  const words = tags('check_in_context').length;
  const note = $('#context-note');
  if (!note) return;
  const model = !!F('judge_url').value.trim();
  const these = `${plural(words, 'word')} here ${words === 1 ? 'is' : 'are'}`;
  if (!words) note.textContent = 'Empty: in a file with no subtitles, every word is simply muted.';
  else if (!model) note.textContent = `${these} checked against the subtitles like every word, and muted in a file with none.`;
  else note.textContent = `${these} checked against the subtitles like every word, and sent to the model below in a file with none.`;
}

/* One set of Plex fields and one set of Jellyfin fields, moved to whichever
   section needs them: two inputs bound to one setting drift apart. */
function placeServerFields() {
  const lib = radio('library_source') || 'sonarr';
  const srv = radio('media_server') || 'none';
  ['plex', 'jellyfin'].forEach((name) => {
    const block = $(`#server-${name}`);
    if (lib === name) { $('#lib-server').appendChild(block); block.hidden = false; }
    else if (srv === name) { $('#media-server-fields').appendChild(block); block.hidden = false; }
    else block.hidden = true;
  });
  $('#server-shared').hidden = !(srv !== 'none' && srv === lib);
}

function renderLibrarySource() {
  const which = radio('library_source') || 'sonarr';
  $('#lib-arr').hidden = which !== 'sonarr';
  $('#lib-server').hidden = which === 'sonarr';
  placeServerFields();
  $('#path-note').textContent = which === 'sonarr'
    ? 'TV paths come from Sonarr, film paths from your media server.'
    : `${which === 'plex' ? 'Plex' : 'Jellyfin'} reports paths as it sees them, so if it runs in a container too, both need the same mount.`;
}

function renderMediaServer() {
  const which = radio('media_server') || 'none';
  placeServerFields();
  $('#hold-row').hidden = which === 'none';
  const name = { plex: 'Plex', jellyfin: 'Jellyfin' }[which] || 'your media server';
  const select = F('hold_policy');
  const keep = select.value || state.settings?.hold_policy || 'video_transcode';
  select.innerHTML = [
    ['never', 'Never wait: censor whenever there is work'],
    ['video_transcode', `Wait while ${name} transcodes video (it needs the GPU)`],
    ['any_transcode', `Wait while ${name} transcodes anything`],
    ['playing', 'Wait while anything at all is playing'],
  ].map(([key, label]) => `<option value="${key}">${esc(label)}</option>`).join('');
  select.value = keep;
}

function renderAsrBackend() {
  const remote = radio('asr_backend') === 'remote';
  $('#asr-remote').hidden = !remote;
  $('#asr-builtin').hidden = remote;
}

function clearErrors() {
  $$('[data-error-for]', form).forEach((p) => { p.textContent = ''; });
  $$('[aria-invalid]', form).forEach((el) => el.removeAttribute('aria-invalid'));
}
function clearError(name) {
  const p = $(`[data-error-for="${CSS.escape(name)}"]`);
  if (p) p.textContent = '';
  if (F(name)) { F(name).removeAttribute('aria-invalid'); F(name).removeAttribute('aria-describedby'); }
}
function showErrors(errors) {
  clearErrors();
  let first = null;
  Object.entries(errors).forEach(([name, message]) => {
    const p = $(`[data-error-for="${CSS.escape(name)}"]`);
    const input = F(name);
    if (p) { p.textContent = message; p.id = p.id || `err-${name.replace('.', '-')}`; }
    if (input && input.setAttribute) {
      input.setAttribute('aria-invalid', 'true');
      if (p) input.setAttribute('aria-describedby', p.id);
    }
    if (!first) first = input && input.focus ? input : p;
  });
  // Hidden sections cannot take focus: show the one the error is in.
  if (errors.asr_url) { $$('input[name="asr_backend"]').forEach((r) => { r.checked = r.value === 'remote'; }); renderAsrBackend(); }
  if (first) {
    first.scrollIntoView({ block: 'center' });
    if (first.focus) first.focus({ preventScroll: true });
  }
}

async function saveSettings() {
  const btn = $('#settings-save');
  setBusy(btn, true);
  const before = state.settings?.library_source;
  try {
    await api('/api/settings', { method: 'PUT', body: collect() });
  } catch (err) {
    setBusy(btn, false);
    if (err.errors) {
      const n = Object.keys(err.errors).length;
      $('#save-bar').classList.add('error');
      $('#save-state').textContent = `${plural(n, 'thing')} to fix before saving`;
      showErrors(err.errors);
    } else fail(err);
    return false;
  }
  setBusy(btn, false);
  settingsDirty = false;
  $('#save-bar').hidden = true;
  $('#save-bar').classList.remove('error');
  toast('Settings saved');
  form._filled = false;
  const fresh = await loadSettings({ force: true });
  if (fresh.library_source !== before) {
    state.shows = null; state.movies = null; state.home = null; state.calendar = null; state.wanted = null;
  }
  return true;
}
form.addEventListener('submit', (e) => { e.preventDefault(); saveSettings(); });

function discardSettings() {
  if (state.settings) fillSettings(state.settings);
}
$('#settings-discard').addEventListener('click', discardSettings);

/* Which section is on screen, for the index. */
if ('IntersectionObserver' in window) {
  const seen = new Map();
  const io = new IntersectionObserver((entries) => {
    entries.forEach((en) => seen.set(en.target.id, en.intersectionRatio));
    let best = null;
    let ratio = 0;
    seen.forEach((r, id) => { if (r > ratio) { ratio = r; best = id; } });
    $$('.settings-index a').forEach((a) => {
      if (best && `s-${a.dataset.section}` === best) a.setAttribute('aria-current', 'true');
      else a.removeAttribute('aria-current');
    });
  }, { threshold: [0, 0.2, 0.5, 0.8] });
  $$('.panel').forEach((p) => io.observe(p));
}

/* ---------------------------------------------------------- test buttons */

function showResult(el, ok, text) {
  el.className = `test-result ${ok ? 'good' : 'bad'}`;
  el.textContent = text;
}

$$('[data-test]').forEach((btn) => btn.addEventListener('click', async () => {
  const service = btn.dataset.test;
  const out = $(`[data-result="${service}"]`);
  const fields = {
    sonarr: ['sonarr.url', 'sonarr.api_key'],
    radarr: ['radarr.url', 'radarr.api_key'],
    plex: ['plex_url', 'plex_token'], jellyfin: ['jellyfin_url', 'jellyfin_api_key'],
  }[service];
  out.className = 'test-result';
  out.textContent = 'Testing…';
  setBusy(btn, true);
  try {
    const r = await api(`/api/settings/test/${service}`, { method: 'POST',
      body: { url: F(fields[0]).value.trim(), api_key: F(fields[1]).value.trim() } });
    showResult(out, r.ok, r.ok ? `Connected to ${r.app} ${r.version || ''}`.trim() : r.error);
  } catch (err) {
    showResult(out, false, err.message);
  } finally { setBusy(btn, false); }
}));

$('#asr-test').addEventListener('click', async (e) => {
  const out = $('#asr-result');
  out.className = 'test-result';
  out.textContent = 'Sending a second of silence…';
  setBusy(e.currentTarget, true);
  try {
    const r = await api('/api/asr/test', { method: 'POST', body: {
      url: F('asr_url').value.trim(), model: F('asr_remote_model').value.trim(), api_key: F('asr_api_key').value.trim() } });
    showResult(out, r.ok, r.ok ? `Works: ${r.note}` : r.error);
  } catch (err) { showResult(out, false, err.message); } finally { setBusy(e.currentTarget, false); }
});

$('#recheck-test').addEventListener('click', async (e) => {
  const out = $('#recheck-result');
  if (!F('recheck_url').value.trim()) { showResult(out, false, 'Put the server address in first.'); return; }
  out.className = 'test-result';
  out.textContent = 'Sending a second of silence…';
  setBusy(e.currentTarget, true);
  try {
    const r = await api('/api/asr/test', { method: 'POST', body: { which: 'recheck',
      url: F('recheck_url').value.trim(), model: F('recheck_model').value.trim(), api_key: F('recheck_api_key').value.trim() } });
    showResult(out, r.ok, r.ok ? `Works: ${r.note}` : r.error);
  } catch (err) { showResult(out, false, err.message); } finally { setBusy(e.currentTarget, false); }
});

$('#asr-list').addEventListener('click', async (e) => {
  const out = $('#asr-result');
  if (!F('asr_url').value.trim()) { showResult(out, false, 'Put the server address in first.'); return; }
  out.className = 'test-result';
  out.textContent = 'Asking…';
  setBusy(e.currentTarget, true);
  try {
    const r = await api('/api/asr/remote-models', { method: 'POST',
      body: { url: F('asr_url').value.trim(), api_key: F('asr_api_key').value.trim() } });
    $('#asr-model-options').innerHTML = (r.models || []).map((m) => `<option value="${esc(m)}">`).join('');
    showResult(out, r.ok, r.ok && r.models.length
      ? `${plural(r.models.length, 'model')}: pick one from the model box`
      : (r.ok ? 'It lists no models; type the name yourself.' : `Could not list them (${r.error}). Type the name yourself.`));
  } catch (err) { showResult(out, false, err.message); } finally { setBusy(e.currentTarget, false); }
});

async function checkJudge() {
  const out = $('#judge-result');
  const btn = $('#judge-check');
  $('#judge-pull').hidden = true;
  if (!F('judge_url').value.trim()) { showResult(out, false, 'Put the address in first.'); return; }
  out.className = 'test-result';
  out.textContent = 'Asking…';
  setBusy(btn, true);
  try {
    const r = await api('/api/judge/check', { method: 'POST',
      body: { url: F('judge_url').value.trim(), model: F('judge_model').value.trim() } });
    if (!r.reachable) showResult(out, false, `Could not reach it: ${r.error || 'no answer'}`);
    else if (r.has_selected) showResult(out, true, `${r.api === 'openai' ? 'The server' : 'Ollama'} has ${r.selected}.`);
    else {
      showResult(out, false, `Reachable, but it does not have ${r.selected}.${r.can_pull ? '' : ` It offers: ${r.models.slice(0, 5).join(', ') || 'nothing'}.`}`);
      $('#judge-pull').hidden = !r.can_pull || settingsDirty;
      if (r.can_pull && settingsDirty) out.textContent += ' Save first, then download it here.';
    }
  } catch (err) { showResult(out, false, err.message); } finally { setBusy(btn, false); }
}
$('#judge-check').addEventListener('click', checkJudge);

$('#judge-pull').addEventListener('click', async () => {
  const out = $('#judge-result');
  try {
    const r = await api('/api/judge/pull', { method: 'POST', body: {} });
    $('#judge-pull').hidden = true;
    out.className = 'test-result';
    out.textContent = `Ollama is downloading ${r.model || 'the model'}. It carries on if you leave this page.`;
    const timer = setInterval(async () => {
      const s = await api('/api/judge/pull').catch(() => ({}));
      if (!s.downloading) {
        clearInterval(timer);
        if (s.error) showResult(out, false, `The download failed: ${s.error}`);
        else checkJudge();
      }
    }, 4000);
  } catch (err) { showResult(out, false, err.message); }
});

/* ----------------------------------------------------------- the model */

async function loadModels() {
  let data;
  try { data = await api('/api/models'); } catch (err) { return; }
  $('#model-list').innerHTML = data.items.map((m) => `
    <div class="model">
      <div class="grow">
        <div class="title">${esc(m.name)} ${m.name === data.selected ? '<span class="badge auto">In use</span>' : ''}</div>
        <div class="sub">${m.ready ? `Downloaded · ${size(m.bytes)}` : `Not downloaded · about ${esc(m.approx_size || 'unknown size')}`}</div>
        ${m.error ? `<div class="hint bad" role="alert">${esc(m.error)}</div>` : ''}
      </div>
      ${m.downloading
        ? `<div class="dl"><div class="progress" role="progressbar" aria-label="Downloading ${esc(m.name)}"
             aria-valuemin="0" aria-valuemax="100" ${m.percent !== undefined ? `aria-valuenow="${m.percent}"` : ''}>
             <span style="width:${m.percent ?? 5}%"></span></div>
           <span class="hint">${m.percent !== undefined ? `${m.percent}% · ${size(m.bytes)} of ${size(m.total)}` : `Downloading… ${size(m.bytes)}`}</span></div>`
        : (m.ready ? `<span class="badge done">${icon('check')}Ready</span>`
          : `<button type="button" class="btn sm" data-action="download-model" data-name="${esc(m.name)}">${icon('download')}${m.error ? 'Try again' : 'Download'}</button>`)}
    </div>`).join('')
    + `<p class="hint">Kept in <code>${esc(data.folder)}</code>, so it survives updating the container.
       Downloading it now saves a long wait on the first censor run.</p>`;
  clearTimeout(loadModels._t);
  if (data.items.some((m) => m.downloading)) loadModels._t = setTimeout(loadModels, 1500);
  else if (loadModels._was) loadSetup();
  loadModels._was = data.items.some((m) => m.downloading);
}
ACTIONS['download-model'] = async (el) => {
  setBusy(el, true);
  await api(`/api/models/${encodeURIComponent(el.dataset.name)}/download`, { method: 'POST', body: {} });
  toast('Downloading. It carries on if you leave this page.');
  loadModels();
};

let cudaPresent = null;
async function loadHardware() {
  try { cudaPresent = (await api('/api/hardware')).cuda_available; } catch (err) { return; }
  renderHardwareNote();
}
function renderHardwareNote() {
  const note = $('#hardware-note');
  if (cudaPresent === null) return;
  const chosen = F('device').value;
  const amd = 'An AMD card cannot be used here; point Cens-arr at your own Whisper server instead.';
  let msg; let bad = false;
  if (chosen === 'cuda' && !cudaPresent) {
    msg = `No NVIDIA GPU is visible to this container, so every job would fail. Choose CPU or "whatever is available". ${amd}`;
    bad = true;
  } else if (chosen === 'cuda') msg = 'Listening runs on the NVIDIA GPU.';
  else if (chosen === 'cpu') msg = cudaPresent ? 'An NVIDIA GPU is here, but the CPU is chosen: expect minutes rather than seconds per episode.' : 'Listening runs on the CPU.';
  else msg = cudaPresent ? 'An NVIDIA GPU was found, so listening runs on it.' : `No NVIDIA GPU found, so listening runs on the CPU. ${amd}`;
  note.className = bad ? 'hint bad' : 'hint';
  note.textContent = msg;
}

async function mediaNow() {
  const out = $('#plex-now');
  if ((radio('media_server') || 'none') === 'none') { out.textContent = ''; return; }
  try {
    const data = await api('/api/media/sessions');
    out.textContent = data.sessions.length
      ? `Right now: ${data.sessions.map((s) => s.description).join('; ')}${data.holding ? ' (the queue is waiting)' : ''}.`
      : 'Nothing is playing right now.';
  } catch (err) { out.textContent = ''; }
}

/* ----------------------------------------------------------- the paths */

async function refreshPathReport() {
  const host = $('#path-report');
  host.innerHTML = '<p class="hint">Checking where your media is…</p>';
  let data;
  try { data = await api('/api/paths'); } catch (err) {
    host.innerHTML = `<p class="hint bad">Could not check: ${esc(err.message)}</p>`;
    return;
  }
  if (data.error) { host.innerHTML = `<p class="hint bad">${esc(data.error)}</p>`; return; }
  if (!data.roots.length) {
    host.innerHTML = '<p class="hint">No library folders reported yet. Save the library details above, then check again.</p>';
    return;
  }
  const bad = data.roots.filter((r) => !r.ok);
  host.innerHTML = `<table class="paths-table">
    <thead><tr><th class="state-cell"><span class="sr-only">State</span></th><th>Library</th><th>Reported path</th><th>From this container</th></tr></thead>
    <tbody>${data.roots.map((r) => `<tr>
      <td class="state-cell ${r.ok ? 'ok' : 'bad'}">${r.ok ? icon('check') : icon('x')}<span class="sr-only">${r.ok ? 'reachable' : 'not reachable'}</span></td>
      <td>${esc(r.library || '')}</td><td><code>${esc(r.path)}</code></td>
      <td>${r.ok ? 'Opens' : (r.elsewhere
        ? `Not here, but it looks mounted at <code>${esc(r.elsewhere)}</code>. Mount it as <code>${esc(r.path)}</code> instead.`
        : 'Not mounted in this container')}</td></tr>`).join('')}</tbody></table>
    ${bad.length
      ? `<p class="hint bad">${bad.length} of ${data.roots.length} folders cannot be opened here, so jobs for them fail with “not found”.
         In docker-compose, a line like <code>- /your/media:${esc(bad[0].path)}</code> fixes it.</p>`
      : '<p class="hint good">Every library folder opens from this container.</p>'}
    <p class="hint">This container can see: ${data.visible.map((v) => `<code>${esc(v)}</code>`).join(' ') || 'nothing mounted'}</p>`;
}
$('#path-recheck').addEventListener('click', () => { refreshPathReport(); loadSetup(); });

/* --------------------------------------------------------- who can use it */

function renderSecurity() {
  const auth = window.__auth || {};
  $('#security').innerHTML = auth.configured ? `
    <p>Signed in as <strong>${esc(auth.username)}</strong>. Everyone who opens Cens-arr needs this password.</p>
    <form id="sec-form" class="fields two" novalidate>
      <div class="field"><label for="sec-current">Current password</label>
        <input id="sec-current" type="password" autocomplete="current-password"></div>
      <div class="field"><label for="sec-new">New password</label>
        <input id="sec-new" type="password" autocomplete="new-password" minlength="8">
        <p class="hint">At least 8 characters. Changing it signs out every other browser.</p></div>
      <div class="field"><label for="sec-user">New username <span class="muted">(optional)</span></label>
        <input id="sec-user" autocomplete="username" autocapitalize="none" placeholder="${esc(auth.username)}"></div>
    </form>
    <div class="test-row">
      <button type="button" class="btn sm" data-action="sec-change">Change password</button>
      <button type="button" class="btn ghost sm" data-action="sec-off">Turn the login off</button>
      <button type="button" class="btn ghost sm" data-signout>Sign out</button>
      <span class="test-result" id="sec-result" role="status"></span>
    </div>`
    : `<div class="callout">${icon('info')}<p><strong>Anyone who can reach this address can use it.</strong>
        Fine on a home network you trust. Set a password if it is reachable from anywhere else.</p></div>
    <form id="sec-form" class="fields two" novalidate>
      <div class="field"><label for="sec-user">Username</label>
        <input id="sec-user" autocomplete="username" autocapitalize="none" minlength="3"></div>
      <div class="field"><label for="sec-new">Password</label>
        <input id="sec-new" type="password" autocomplete="new-password" minlength="8">
        <p class="hint">At least 8 characters.</p></div>
    </form>
    <div class="test-row"><button type="button" class="btn sm" data-action="sec-create">Turn the login on</button>
      <span class="test-result" id="sec-result" role="status"></span></div>`;
}

ACTIONS['sec-create'] = async () => {
  const username = $('#sec-user').value.trim();
  const password = $('#sec-new').value;
  try {
    await api('/api/auth/setup', { method: 'POST', body: { username, password } });
  } catch (err) { showResult($('#sec-result'), false, err.message); return; }
  window.__auth = { configured: true, username };
  $('#more-signout').hidden = false;
  renderSecurity();
  toast('Login is on');
  loadSetup();
};
ACTIONS['sec-change'] = async () => {
  try {
    const data = await api('/api/auth/change', { method: 'POST', body: {
      current: $('#sec-current').value, password: $('#sec-new').value, username: $('#sec-user').value.trim() } });
    window.__auth = { configured: true, username: data.username };
    renderSecurity();
    toast('Password changed. Other browsers are signed out.');
  } catch (err) { showResult($('#sec-result'), false, err.message); }
};
ACTIONS['sec-off'] = async () => {
  const current = $('#sec-current').value;
  if (!current) { showResult($('#sec-result'), false, 'Type the current password first.'); $('#sec-current').focus(); return; }
  const answer = await ask('Turn the login off?',
    'Anyone who can reach this address will be able to use it without a password.',
    [{ label: 'Turn it off', value: 'yes', primary: true }], { danger: true });
  if (answer !== 'yes') return;
  try {
    await api('/api/auth/change', { method: 'POST', body: { current, disable: true } });
  } catch (err) { showResult($('#sec-result'), false, err.message); return; }
  window.__auth = { configured: false, username: '' };
  $('#more-signout').hidden = true;
  renderSecurity();
  toast('Login turned off');
  loadSetup();
};
document.addEventListener('click', async (e) => {
  if (!e.target.closest('[data-signout]')) return;
  await api('/api/auth/logout', { method: 'POST', body: {} }).catch(() => {});
  if ($('#more-sheet').open) $('#more-sheet').close();
  showGate('login');
});

/* ======================================================================
   The phone's More menu
   ====================================================================== */

$('#more-button').addEventListener('click', () => $('#more-sheet').showModal());
$('#more-sheet').addEventListener('click', (e) => {
  if (e.target === e.currentTarget || e.target.closest('a')) $('#more-sheet').close();
});

/* ======================================================================
   Signing in
   ====================================================================== */

let gateMode = 'login';

function showGate(mode) {
  gateMode = mode;
  const setup = mode === 'setup';
  $('#gate-blurb').textContent = setup
    ? 'Pick a username and password. You will need them each time you open Cens-arr.'
    : 'Sign in to continue.';
  $('#gate-submit').textContent = setup ? 'Create account' : 'Sign in';
  $('#gate-confirm-row').hidden = !setup;
  $('#gate-pass').autocomplete = setup ? 'new-password' : 'current-password';
  $('#gate-error').hidden = true;
  $('#gate').hidden = false;
  $('.shell').inert = true;
  setTimeout(() => $('#gate-user').focus(), 30);
}

function hideGate() {
  $('#gate').hidden = true;
  $('.shell').inert = false;
  $('#gate-pass').value = '';
  $('#gate-confirm').value = '';
}

$('#gate-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = $('#gate-error');
  error.hidden = true;
  const username = $('#gate-user').value.trim();
  const password = $('#gate-pass').value;
  if (!username || !password) {
    error.textContent = 'Enter a username and password.';
    error.hidden = false;
    return;
  }
  if (gateMode === 'setup' && password !== $('#gate-confirm').value) {
    error.textContent = 'Those two passwords are not the same.';
    error.hidden = false;
    return;
  }
  const btn = $('#gate-submit');
  setBusy(btn, true);
  try {
    await api(`/api/auth/${gateMode === 'setup' ? 'setup' : 'login'}`, { method: 'POST', body: { username, password } });
    hideGate();
    boot({ signedIn: true });
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally { setBusy(btn, false); }
});

async function checkAuth() {
  let auth;
  try { auth = await (await fetch('/api/auth/state')).json(); } catch (err) { return true; }
  window.__auth = auth;
  if (!auth.configured) return true;
  const probe = await fetch('/api/jobs');
  if (probe.status === 401) { showGate('login'); return false; }
  return true;
}

/* ======================================================================
   Installable app, and starting up
   ====================================================================== */

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/static/sw.js', { scope: '/' })
      .catch((err) => console.info('[Cens-arr] no service worker:', err.message));
  });
}

let timers = [];
async function boot({ signedIn = false } = {}) {
  noteSource({ library_source: state.libSource });
  if (!signedIn && !(await checkAuth())) return;
  timers.forEach(clearInterval);
  // The page first, so it is never blank; the queue's numbers follow.
  route();
  poll();
  loadSettings({ fill: false }).catch(() => {});
  timers = [
    setInterval(() => { if ($('#gate').hidden && !document.hidden) poll(); }, 3000),
    // Home asks the library, so it refreshes slowly and only while shown.
    setInterval(() => { if (state.view === 'home' && !document.hidden) loadHome(); }, 60000),
  ];
}

boot();
