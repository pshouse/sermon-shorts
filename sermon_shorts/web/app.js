// Sermon Shorts Studio: the review app. Plain JavaScript, no build step,
// served by the API at /app/. Talks only to /api/v1 on the same origin.
//
// Everything shown comes from transcripts, Claude or people, so the page is
// built with h() below, which sets text, never HTML. The server's
// Content-Security-Policy refuses any inline script or style as a backstop.
'use strict';

const API = '/api/v1';
const REPO = 'https://github.com/pshouse/sermon-shorts';
const PLATFORMS = ['youtube', 'facebook', 'instagram'];
const PLATFORM_NAMES = { youtube: 'YouTube', facebook: 'Facebook', instagram: 'Instagram' };
const STEP_NAMES = {
  fetch: 'Fetched from Subsplash', transcribe: 'Transcribed', find_sermon: 'Found the sermon',
  trim: 'Trimmed to the sermon', select: 'Picked and reviewed moments', render: 'Rendered a clip',
  done: 'Done',
};
const state = { me: null, renderSeq: 0, cleanup: [] };

// --- building the page -------------------------------------------------------

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'text') el.textContent = v;
    else if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked' || k === 'disabled') el[k] = !!v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function svgPlay() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('width', '14'); svg.setAttribute('height', '14');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', 'M7 4l13 8-13 8z'); path.setAttribute('fill', 'none');
  path.setAttribute('stroke', '#FFFFFF'); path.setAttribute('stroke-width', '2.5');
  path.setAttribute('stroke-linejoin', 'round');
  svg.append(path);
  return svg;
}

const pill = (text, tone) => h('span', { class: `pill ${tone || ''}` }, text);

function toast(message, tone) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.className = `toast show ${tone || ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.className = 'toast'; }, 5000);
}

function setMain(...kids) {
  const main = document.getElementById('main');
  main.replaceChildren(...kids.flat());
}

// --- formatting ----------------------------------------------------------------

function when(iso) {
  if (!iso) return '-';
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z');
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit' });
}
function serviceDate(ymd) {
  if (!ymd) return '';
  const d = new Date(ymd + 'T12:00:00');
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}
function took(a, b) {
  if (!a || !b) return '';
  const s = Math.max(0, Math.round((new Date(b) - new Date(a)) / 1000));
  return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`
    : s >= 60 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`;
}
function mmss(sec) {
  if (sec === null || sec === undefined) return '-';
  const s = Math.round(sec);
  const hms = s >= 3600 ? [Math.floor(s / 3600), String(Math.floor(s % 3600 / 60)).padStart(2, '0')]
    : [Math.floor(s / 60)];
  return `${hms.join(':')}:${String(s % 60).padStart(2, '0')}`;
}
const STATE_TONE = { approved: 'ok', rejected: 'bad', stale: 'warn', pending: '' };
const STATE_TEXT = { approved: 'Approved', rejected: 'Rejected', stale: 'Changed since approval',
  pending: 'Waiting for review' };
const RUN_TONE = { succeeded: 'ok', failed: 'bad', running: 'info', starting: 'info', skipped: '' };
const can = (perm) => !!state.me && state.me.can.includes(perm);

// One line on where a clip stands, with the decision's time in local format.
function reviewLine(c) {
  const d = c.decision;
  if (c.review_state === 'approved' || c.review_state === 'rejected') {
    const verb = c.review_state === 'approved' ? 'Approved' : 'Rejected';
    const late = d.backfilled_from ? ' (recorded later)' : '';
    return `${verb} by ${d.by_login}, ${when(d.backfilled_from || d.at)}${late}${d.note ? ` ("${d.note}")` : ''}`;
  }
  if (c.review_state === 'stale') return `Changed since ${d.by_login} approved it; needs approving again`;
  return d ? 'Changed since it was rejected; waiting for review' : '';
}

// --- talking to the API --------------------------------------------------------

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

async function api(path, { method = 'GET', body, allow401 = false } = {}) {
  const init = { method, credentials: 'same-origin',
    headers: { 'X-Requested-With': 'sermon-shorts', Accept: 'application/json' } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const r = await fetch(API + path, init);
  const isJson = (r.headers.get('content-type') || '').includes('json');
  const data = isJson ? await r.json() : await r.text();
  if (r.status === 401 && !allow401) {
    state.me = null;
    go('#/signin');
  }
  if (!r.ok) {
    const err = (isJson && data && data.error) || {};
    throw new ApiError(r.status, err.code || `http_${r.status}`, err.message || `Request failed (${r.status}).`);
  }
  return data;
}

// Run an action from a button: disable it, report errors, refresh on success.
async function act(button, fn, { done, refresh = true } = {}) {
  if (button) button.disabled = true;
  try {
    await fn();
    if (done) toast(done);
    if (refresh) await render();
  } catch (e) {
    toast(e.message, 'bad');
    if (e.code === 'approval_stale' || e.code === 'plan_changed') await render();
  } finally {
    if (button && button.isConnected) button.disabled = false;
  }
}

// --- routing -------------------------------------------------------------------

function go(hash) {
  if (location.hash === hash) render(); else location.hash = hash;
}

function route() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, query] = raw.split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  return { page: parts[0] || 'week', args: parts.slice(1), query: new URLSearchParams(query || '') };
}

async function render() {
  const seq = ++state.renderSeq;
  state.cleanup.forEach((fn) => fn());
  state.cleanup = [];
  const r = route();
  const pages = { week: pageWeek, review: pageReview, publish: pagePublish, history: pageHistory,
    runs: pageRun, people: pagePeople, signin: pageSignIn, join: pageJoin };
  const page = pages[r.page] || pageWeek;
  if (!state.me && page !== pageSignIn && page !== pageJoin) {
    try { state.me = await api('/me', { allow401: true }); } catch { state.me = null; }
    if (!state.me) { renderTopbar(r.page); return go('#/signin'); }
  }
  renderTopbar(r.page);
  refreshVersion();
  try {
    const content = await page(r);
    if (seq !== state.renderSeq || !content) return;  // a newer navigation won, or it redirected
    setMain(content);
  } catch (e) {
    if (seq !== state.renderSeq || e.status === 401) return;
    setMain(h('div', { class: 'card' }, h('h2', { text: "Couldn't load this page" }),
      h('p', { class: 'muted', text: e.message })));
  }
}

// --- what's running ---------------------------------------------------------

async function refreshVersion() {
  if (!state.health || Date.now() - state.healthAt > 60000) {
    try { state.health = await api('/health', { allow401: true }); state.healthAt = Date.now(); }
    catch { return; }
  }
  const v = state.health;
  const link = (href, text, cls) => h('a', { href, target: '_blank', rel: 'noopener noreferrer', class: cls, text });
  document.getElementById('footer').replaceChildren(
    h('span', { text: `Sermon Shorts ${v.version}` }),
    v.commit ? [' · ', link(`${REPO}/commit/${v.commit}`, v.commit + (v.dirty ? ' (modified)' : ''), 'mono')] : null,
    ' · ', link(`${REPO}/blob/main/CHANGELOG.md`, "What's new"));
  // Owners run the server: tell them when it's behind the code on disk.
  const disk = v.on_disk || {};
  const behind = can('run') && disk.restart_needed;
  document.getElementById('notice').replaceChildren(behind ? h('div', { class: 'notice' },
    h('strong', { text: 'Restart needed. ' }),
    `The server is running ${v.label}, but newer code is on the sprite ` +
    `(${disk.version && disk.version !== v.version ? `${disk.version}, ` : ''}${disk.commit}) that changes ` +
    `${disk.changed_files.length} Python file${disk.changed_files.length === 1 ? '' : 's'}. ` +
    'Restart the api service on the sprite to load it.') : '');
}

function renderTopbar(page) {
  const bar = document.getElementById('topbar');
  const link = (href, text, key) => h('a', { href, 'aria-current': page === key ? 'page' : null }, text);
  const brand = h('a', { class: 'brand', href: '#/' },
    h('span', { class: 'brand-mark' }, svgPlay()),
    h('span', { class: 'brand-name' }, h('b', { text: 'Sermon Shorts' }), h('span', { text: 'Studio' })));
  if (!state.me) return bar.replaceChildren(brand);
  const initials = state.me.name.split(/\s+/).map((w) => w[0] || '').join('').slice(0, 2).toUpperCase();
  bar.replaceChildren(
    brand,
    h('nav', { class: 'nav', 'aria-label': 'Main' },
      link('#/', 'This week', 'week'), link('#/review', 'Review', 'review'),
      link('#/publish', 'Publish', 'publish'), link('#/history', 'History & audit', 'history'),
      link('#/people', 'People', 'people')),
    h('div', { class: 'me' },
      h('span', { class: 'avatar', 'aria-hidden': 'true', text: initials }),
      h('span', {}, `${state.me.name} `, h('span', { class: 'muted small', text: `(${state.me.role})` })),
      h('button', { class: 'link-btn small', onclick: signOut, text: 'Sign out' })));
}

async function signOut() {
  try { await api('/session', { method: 'DELETE' }); } catch { /* signed out anyway */ }
  state.me = null;
  go('#/signin');
}

// --- shared bits ---------------------------------------------------------------

async function loadServices() { return api('/services?limit=20'); }

async function pickService(r) {
  const services = await loadServices();
  const sid = r.args[0] || (services[0] && services[0].id);
  return { services, sid };
}

function noServices() {
  return h('div', { class: 'card' }, h('h2', { text: 'No services yet' }),
    h('p', { class: 'muted' }, 'The weekly run records each Sunday\'s service. Folders made before ' +
      'the Studio existed can be added with: ', h('span', { class: 'mono', text: 'sermon-shorts services import <clips folder>' })));
}

function serviceSwitcher(services, sid, page) {
  if (services.length < 2) return null;
  const id = 'service-pick';
  return h('div', { class: 'field' }, h('label', { for: id, text: 'Service' }),
    h('select', { id, onchange: (e) => go(`#/${page}/${encodeURIComponent(e.target.value)}`) },
      services.map((s) => h('option', { value: s.id, selected: s.id === sid ? 'selected' : null },
        `${s.date || ''} ${s.title || s.id}`.trim()))));
}

function serviceHead(svc, title, extra) {
  const bits = [svc.speaker, svc.sermon_title && svc.sermon_title !== svc.title ? svc.sermon_title : null]
    .filter(Boolean);
  return h('div', { class: 'head' },
    h('div', { class: 'head-text' },
      h('span', { class: 'eyebrow', text: svc.date ? `Service of ${serviceDate(svc.date)}` : svc.id }),
      h('h1', { text: title || svc.title || svc.id }),
      bits.length ? h('span', { class: 'muted', text: bits.join(' · ') }) : null),
    h('div', { class: 'head-actions' }, extra));
}

function coverImg(clip, cls) {
  return clip.cover_url
    ? h('img', { class: `cover ${cls}`, src: clip.cover_url, alt: '' })
    : h('span', { class: `cover ${cls}`, 'aria-hidden': 'true' });
}

function postPills(clip) {
  return PLATFORMS.map((p) => {
    const post = clip.posts[p];
    if (post) return pill(post.cover === 'failed' ? `${PLATFORM_NAMES[p]} live, no cover` : `${PLATFORM_NAMES[p]} live`,
      post.cover === 'failed' ? 'warn' : '');
    if (p === 'instagram' && clip.instagram_needs_manual && clip.review_state === 'approved')
      return pill('Instagram: post by hand', 'warn');
    return null;
  });
}

function needsLink(svc, item) {
  if (item.kind === 'review') return [`#/review/${encodeURIComponent(svc.id)}/${encodeURIComponent(item.file)}`, 'Review'];
  if (item.kind === 'run') return ['#/history', 'See runs'];
  return [`#/publish/${encodeURIComponent(svc.id)}`, 'Open in Publish'];
}

function needsYou(svc) {
  if (!svc.needs_you.length) return null;
  return h('section', { class: 'needs', 'aria-labelledby': 'needs-h' },
    h('h2', { id: 'needs-h', text: `Needs you · ${svc.needs_you.length}` }),
    svc.needs_you.map((item) => {
      const [href, label] = needsLink(svc, item);
      return h('div', { class: 'needs-item' }, h('span', { text: item.text }),
        h('a', { href, class: 'small', text: label }));
    }));
}

function stepList(run, events) {
  const items = [];
  let prev = run.started_at;
  events.forEach((e, i) => {
    const last = i === events.length - 1;
    if (e.state === 'start' && !(last && run.status === 'running')) { prev = e.t; return; }
    const live = e.state === 'start';
    const detail = Object.entries(e.data || {}).filter(([, v]) => v !== null && v !== '')
      .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`).join(' · ');
    items.push(h('li', { class: 'step' },
      h('span', { class: `step-dot ${live ? 'live' : ''}`, 'aria-hidden': 'true', text: String(items.length + 1) }),
      h('span', {}, h('strong', { text: (STEP_NAMES[e.step] || e.step) + (live ? ' (in progress)' : '') }),
        detail ? h('br') : null, detail ? h('span', { class: 'muted small', text: detail }) : null),
      h('span', { class: 'mono muted', text: live ? '' : took(prev, e.t) })));
    prev = e.t;
  });
  if (!items.length) return h('p', { class: 'muted', text: 'No steps recorded yet.' });
  return h('ol', { class: 'list' }, items);
}

// --- This week -----------------------------------------------------------------

async function pageWeek(r) {
  const { services, sid } = await pickService(r);
  if (!sid) return noServices();
  const svc = await api(`/services/${encodeURIComponent(sid)}`);
  const cands = await api(`/services/${encodeURIComponent(sid)}/candidates`);
  const run = svc.latest_run ? await api(`/runs/${encodeURIComponent(svc.latest_run.id)}`) : null;

  const approved = svc.clips.filter((c) => c.review_state === 'approved').length;
  const live = svc.clips.reduce((n, c) => n + Object.keys(c.posts).length, 0);
  const byPlatform = PLATFORMS.map((p) => `${PLATFORM_NAMES[p]} ${svc.clips.filter((c) => c.posts[p]).length}`);
  const below = cands.filter((c) => c.outcome !== 'rendered').length;

  const runButton = can('run') ? h('button', { class: 'btn', onclick: (e) => act(e.currentTarget, async () => {
    if (!confirm('Start the weekly run now? It fetches the newest service and makes its clips.')) return;
    const { run_id: id } = await api('/runs', { method: 'POST', body: {} });
    go(`#/runs/${encodeURIComponent(id)}`);
  }, { refresh: false }), text: 'Run now' }) : null;

  return h('div', { class: 'page-body' },
    serviceHead(svc, null, [serviceSwitcher(services, sid, 'week'), runButton]),
    h('div', { class: 'stats' },
      h('div', { class: 'stat' }, h('span', { class: 'muted small', text: 'Clips rendered' }),
        h('b', { text: String(svc.clips.length) }),
        h('span', { class: 'muted small', text: cands.length ? `of ${cands.length} candidates; ${below} not rendered` : 'candidates not recorded' })),
      h('div', { class: 'stat' }, h('span', { class: 'muted small', text: 'Approved' }),
        h('b', { text: String(approved) }),
        h('span', { class: 'muted small', text: `${svc.clips.length - approved} not approved` })),
      h('div', { class: 'stat' }, h('span', { class: 'muted small', text: 'Posts live' }),
        h('b', { text: String(live) }),
        h('span', { class: 'muted small', text: byPlatform.join(' · ') }))),
    needsYou(svc),
    h('div', { class: 'row' },
      run ? h('section', { class: 'card grow', 'aria-labelledby': 'run-h' },
        h('div', { class: 'card-head' }, h('h2', { id: 'run-h', text: 'Latest run' }),
          pill(run.status, RUN_TONE[run.status])),
        h('p', { class: 'muted small', text: `${when(run.started_at)} · ${took(run.started_at, run.finished_at) || 'running'} · started by ${run.started_by}` }),
        stepList(run, run.events),
        h('a', { href: `#/runs/${encodeURIComponent(run.id)}`, class: 'small', text: 'Run details and log' }))
        : null,
      h('section', { class: 'card grow', 'aria-labelledby': 'clips-h' },
        h('div', { class: 'card-head' }, h('h2', { id: 'clips-h', text: 'Clips' }),
          h('a', { href: `#/review/${encodeURIComponent(sid)}`, class: 'small', text: 'Review all' })),
        svc.clips.length ? svc.clips.map((c) => h('div', { class: 'clip-row' },
          coverImg(c, 'card-thumb'),
          h('div', { class: 'head-text' },
            h('a', { href: `#/review/${encodeURIComponent(sid)}/${encodeURIComponent(c.file)}` }, h('strong', { text: c.title })),
            h('span', { class: 'muted small', text: `${Math.round(c.seconds)}s · sermon ${mmss(c.start)} to ${mmss(c.end)}` }),
            h('div', { class: 'pills' }, pill(STATE_TEXT[c.review_state], STATE_TONE[c.review_state]), postPills(c)))))
          : h('p', { class: 'muted', text: 'No clips in this service.' }))));
}

// --- Review --------------------------------------------------------------------

async function pageReview(r) {
  const { services, sid } = await pickService(r);
  if (!sid) return noServices();
  const svc = await api(`/services/${encodeURIComponent(sid)}`);
  const cands = await api(`/services/${encodeURIComponent(sid)}/candidates`);
  if (!svc.clips.length) return h('div', {}, serviceHead(svc, 'Review'), h('p', { class: 'muted', text: 'No clips to review.' }));
  const file = r.args[1] || (svc.clips.find((c) => c.review_state !== 'approved') || svc.clips[0]).file;
  const clip = svc.clips.find((c) => c.file === file) || svc.clips[0];
  const detail = await api(`/services/${encodeURIComponent(sid)}/clips/${encodeURIComponent(clip.file)}`);
  const claude = cands.find((c) => c.clip_file === clip.file);
  const dropped = cands.filter((c) => c.outcome !== 'rendered');

  const list = h('aside', { class: 'side', 'aria-label': 'Clips' },
    h('div', { class: 'list-gap' }, svc.clips.map((c) => h('a', {
      class: 'clip-pick', href: `#/review/${encodeURIComponent(sid)}/${encodeURIComponent(c.file)}`,
      'aria-current': c.file === clip.file ? 'true' : null },
    coverImg(c, 'thumb'),
    h('span', { class: 'head-text' }, h('strong', { text: c.title }),
      h('span', { class: 'muted small', text: `${Math.round(c.seconds)}s · pick score ${c.score ?? '-'}` }),
      h('span', {}, pill(STATE_TEXT[c.review_state], STATE_TONE[c.review_state])))))),
    dropped.length ? h('details', { class: 'card tight' },
      h('summary', { text: `Not rendered · ${dropped.length}` }),
      h('p', { class: 'muted small', text: "Moments Claude proposed that didn't make the cut, and why." }),
      h('ul', { class: 'list' }, dropped.map((c) => h('li', {},
        h('div', { class: 'card-head' }, h('strong', { class: 'small', text: c.title }),
          h('span', { class: 'mono muted', text: c.standalone !== null ? `stranger ${c.standalone}` : c.outcome.replace(/_/g, ' ') })),
        c.note ? h('p', { class: 'muted small', text: c.note }) : null))))
      : null);

  const noteBox = h('textarea', { id: 'decision-note', rows: '2', placeholder: clip.review_state === 'approved'
    ? 'e.g. wrong verse on screen' : 'For the record, e.g. checked the verse reference' });
  const err = h('p', { class: 'error', role: 'alert' });
  // Offer only the decisions that change something: an approved clip can be
  // withdrawn, a rejected one approved again, anything else either way.
  const approved = clip.review_state === 'approved';
  const rejected = clip.review_state === 'rejected';
  const liveOn = PLATFORMS.filter((p) => clip.posts[p]).map((p) => PLATFORM_NAMES[p]);
  const liveText = liveOn.join(liveOn.length > 2 ? ', ' : ' and ').replace(/, ([^,]*)$/, ' and $1');
  const decide = (decision) => (e) => {
    const note = noteBox.value.trim();
    if (decision === 'reject' && !note) {
      err.textContent = approved ? 'Say why you are withdrawing the approval.' : 'Say why before rejecting.';
      noteBox.focus();
      return;
    }
    if (decision === 'reject' && liveOn.length &&
        !confirm(`${clip.title} stays posted on ${liveText}. Withdrawing only stops further posting. Withdraw the approval?`)) return;
    act(e.currentTarget, () => api(`/services/${encodeURIComponent(sid)}/clips/${encodeURIComponent(clip.file)}/decisions`,
      { method: 'POST', body: { decision, note, fingerprint: clip.fingerprint } }),
    { done: decision === 'approve' ? 'Approved.' : approved ? 'Approval withdrawn.' : 'Rejected.' });
  };
  const decisionButtons = [
    rejected || !approved ? h('button', { class: 'btn primary', onclick: decide('approve'), text: rejected ? 'Approve instead' : 'Approve' }) : null,
    !rejected ? h('button', { class: 'btn', onclick: decide('reject'), text: approved ? 'Withdraw approval' : 'Reject' }) : null,
  ];
  const decisionPanel = h('div', { class: 'panel' },
    h('div', { class: 'card-head' }, pill(STATE_TEXT[clip.review_state], STATE_TONE[clip.review_state]),
      reviewLine(clip) ? h('span', { class: 'small', text: reviewLine(clip) }) : null),
    h('p', { class: 'muted small' }, 'A decision covers ', h('span', { class: 'mono', text: clip.file }),
      ' exactly as it is now: video, cover, title and description. Any change afterwards needs a new decision.'),
    can('approve') ? [
      approved && liveOn.length ? h('p', { class: 'small warn-note', text:
        `Already posted on ${liveText}. Withdrawing the approval stops any further posting, but those posts stay up: take them down in each app if they need to go.` }) : null,
      h('label', { for: 'decision-note' }, approved ? 'Why ' : 'Note for the record ',
        h('span', { class: 'muted', text: approved ? '(required to withdraw)' : rejected ? '(optional)' : '(required to reject)' })),
      noteBox, err,
      h('div', { class: 'actions' }, decisionButtons)]
      : h('p', { class: 'muted small', text: `As a ${state.me.role} you can watch and leave notes; approving needs an approver.` }));

  const noteInput = h('textarea', { id: 'note-text', rows: '2', placeholder: 'A note for whoever approves' });
  const notes = h('div', { class: 'panel' }, h('h3', { text: 'Notes' }),
    clip.notes.length ? h('ul', { class: 'list' }, clip.notes.map((n) => h('li', {},
      h('p', { text: n.text }), h('p', { class: 'muted small', text: `${n.by_login}, ${when(n.at)}` }))))
      : h('p', { class: 'muted small', text: 'No notes yet.' }),
    can('note') ? [h('label', { for: 'note-text', text: 'Add a note' }), noteInput,
      h('div', { class: 'actions' }, h('button', { class: 'btn small', text: 'Add note', onclick: (e) => {
        if (!noteInput.value.trim()) return;
        act(e.currentTarget, () => api(`/services/${encodeURIComponent(sid)}/clips/${encodeURIComponent(clip.file)}/notes`,
          { method: 'POST', body: { text: noteInput.value } }), { done: 'Note added.' });
      } }))] : null);

  const record = h('div', { class: 'panel' }, h('h3', { text: 'Record for this clip' }),
    h('p', { class: 'muted small', text: 'Newest first. Entries are never edited or removed.' }),
    h('ul', { class: 'list' }, detail.audit.map((e) => h('li', {},
      h('div', { class: 'card-head' }, h('span', {}, h('strong', { text: auditLabel(e.action) }), ` by ${e.actor}`),
        h('span', { class: 'mono muted', text: when(e.at) })),
      auditDetail(e) ? h('p', { class: 'muted small', text: auditDetail(e) }) : null))));

  return h('div', { class: 'page-body' },
    serviceHead(svc, 'Review and approve', serviceSwitcher(services, sid, 'review')),
    h('div', { class: 'row' }, list,
      h('section', { class: 'card main-col', 'aria-labelledby': 'clip-h' },
        h('div', { class: 'detail' },
          h('div', { class: 'head-text' },
            clip.video_url
              ? h('video', { class: 'player', src: clip.video_url, poster: clip.cover_url, controls: true,
                playsinline: true, preload: 'metadata', 'aria-label': `Video: ${clip.title}` })
              : h('div', { class: 'player', role: 'img', 'aria-label': 'Video not available here' }),
            h('span', { class: 'muted small', text: `1080 x 1920 · ${Math.round(clip.seconds)}s · captions burned in` }),
            clip.cover_url ? h('a', { href: clip.cover_url, target: '_blank', rel: 'noopener', class: 'small', text: 'Open the cover image' }) : null),
          h('div', { class: 'detail-text' },
            h('div', { class: 'head-text' }, h('h2', { id: 'clip-h', class: 'clip-title', text: clip.title }),
              h('span', { class: 'muted small', text: `Sermon ${mmss(clip.start)} to ${mmss(clip.end)} · pick score ${clip.score ?? '-'}` +
                (claude && claude.standalone !== null ? ` · stranger score ${claude.standalone}` : '') })),
            h('div', { class: 'field' }, h('span', { class: 'muted small', text: 'Description, as it will post' }),
              h('p', { class: 'box', text: clip.description || '(none)' })),
            claude && claude.note ? h('div', { class: 'field' }, h('span', { class: 'muted small', text: "Claude's review" }),
              h('p', { text: claude.note })) : null,
            decisionPanel, notes, record)))));
}

const AUDIT_LABELS = {
  'clip.approved': 'Approved', 'clip.rejected': 'Rejected', 'clip.noted': 'Note',
  'clip.rendered': 'Rendered', 'clip.imported': 'Imported', 'post.created': 'Posted',
  'post.marked_manual': 'Posted by hand', 'post.failed': 'Post failed', 'post.imported': 'Post imported',
  'cover.set': 'Cover set', 'cover.failed': 'Cover failed', 'publish.planned': 'Previewed a publish',
  'run.finished': 'Run finished', 'run.failed': 'Run failed', 'run.requested': 'Run requested',
  'run.cancel_requested': 'Cancel requested', 'sermon.found': 'Found the sermon',
  'service.imported': 'Service imported', 'user.root_created': 'Root owner created',
  'user.joined': 'Joined', 'user.role_changed': 'Role changed', 'user.removed': 'Removed',
  'invite.created': 'Invited', 'invite.revoked': 'Invite withdrawn', 'token.created': 'Token issued',
  'token.revoked': 'Token revoked',
};
const auditLabel = (action) => AUDIT_LABELS[action] || action;
function auditDetail(e) {
  const skip = new Set(['fingerprint', 'run', 'token', 'post_id']);
  return Object.entries(e.data || {}).filter(([k, v]) => !skip.has(k) && v !== '' && v !== null)
    .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${Array.isArray(v) ? v.join(', ') : v}`).join(' · ');
}

// --- Publish -------------------------------------------------------------------

async function pagePublish(r) {
  const { services, sid } = await pickService(r);
  if (!sid) return noServices();
  const svc = await api(`/services/${encodeURIComponent(sid)}`);
  const base = `/services/${encodeURIComponent(sid)}`;
  const canPublish = can('publish');

  // Choose, preview, confirm.
  const candidates = svc.clips.filter((c) => c.review_state === 'approved' &&
    PLATFORMS.some((p) => !c.posts[p] && !(p === 'instagram' && c.instagram_needs_manual)));
  const boxes = candidates.map((c) => h('input', { type: 'checkbox', id: `pick-${c.n}`, value: c.file, checked: true }));
  const planArea = h('div', {});

  const showPlan = (plan, files) => {
    const posts = plan.posts.map((p) => h('li', {}, h('strong', { text: PLATFORM_NAMES[p.platform] }), ` · ${p.title}`));
    const skipped = plan.skipped.map((s) => h('li', { class: 'muted small',
      text: `${s.title}${s.platform ? ` on ${PLATFORM_NAMES[s.platform]}` : ''}: ${s.reason}` }));
    planArea.replaceChildren(h('div', { class: 'card plan' },
      h('h2', { text: plan.posts.length ? 'About to post publicly' : 'Nothing can be posted' }),
      plan.posts.length ? h('ul', { class: 'list' }, posts) : null,
      skipped.length ? [h('h3', { text: 'Skipped' }), h('ul', { class: 'list' }, skipped)] : null,
      plan.posts.length ? h('div', { class: 'actions' },
        h('button', { class: 'btn danger', text: `Post publicly now (${plan.posts.length})`, onclick: (e) => act(e.currentTarget, async () => {
          const { job_id: job } = await api(`${base}/publish`, { method: 'POST', body: { clips: files, platforms: plan.platforms, plan_id: plan.plan_id } });
          await followJob(job, planArea);
        }, { refresh: false }) }),
        h('button', { class: 'btn', text: 'Cancel', onclick: () => planArea.replaceChildren() })) : null,
      h('p', { class: 'muted small', text: 'Posts go out immediately and publicly. If anything changes before you confirm, the post is refused and you preview again.' })));
    planArea.querySelector('button')?.focus();
  };

  const chooser = canPublish ? h('section', { class: 'card', 'aria-labelledby': 'choose-h' },
    h('h2', { id: 'choose-h', text: 'Ready to post' }),
    candidates.length ? [
      candidates.map((c, i) => h('label', { class: 'check', for: `pick-${c.n}` }, boxes[i], c.title)),
      h('div', { class: 'actions' }, h('button', { class: 'btn primary', text: 'Preview', onclick: (e) => act(e.currentTarget, async () => {
        const files = boxes.filter((b) => b.checked).map((b) => b.value);
        if (!files.length) throw new Error('Pick at least one clip.');
        showPlan(await api(`${base}/publish-plans`, { method: 'POST', body: { clips: files } }), files);
      }, { refresh: false }) }))]
      : h('p', { class: 'muted', text: 'Nothing approved is waiting to be posted. Approved clips show up here.' }),
    planArea)
    : h('div', { class: 'card tight' }, h('p', { class: 'muted', text: `As a ${state.me.role} you can see what's posted; posting needs an approver.` }));

  const clipCards = svc.clips.map((c) => h('section', { class: 'card', 'aria-label': c.title },
    h('div', { class: 'card-head' }, h('h2', { text: c.title }),
      h('span', { class: 'muted small', text: `${Math.round(c.seconds)}s · ${reviewLine(c) || STATE_TEXT[c.review_state]}` })),
    h('div', {}, PLATFORMS.map((p) => platformRow(svc, c, p, base, canPublish)))));

  return h('div', { class: 'page-body' },
    serviceHead(svc, 'Publish and track', serviceSwitcher(services, sid, 'publish')),
    h('p', { class: 'muted', text: 'Only approved clips can be posted, never twice to the same place. Every post is recorded with who made it.' }),
    chooser, clipCards);
}

function platformRow(svc, clip, platform, base, canPublish) {
  const post = clip.posts[platform];
  const name = h('span', { class: 'platform-name', text: PLATFORM_NAMES[platform] });
  const path = `${base}/clips/${encodeURIComponent(clip.file)}/posts`;
  if (post) {
    const how = post.manual ? 'by hand' : post.by_login ? `by ${post.by_login}` : 'before accounts';
    const coverFailed = post.cover === 'failed';
    return h('div', { class: 'platform' }, name,
      pill(coverFailed ? 'Live, no cover' : 'Live', coverFailed ? 'warn' : 'ok'),
      h('span', { class: 'platform-detail' }, `Posted ${how}, ${when(post.at)}. `,
        post.url ? h('a', { href: post.url, target: '_blank', rel: 'noopener noreferrer', class: 'mono', text: post.url.replace(/^https?:\/\//, '') }) : null),
      coverFailed && platform === 'youtube' && canPublish
        ? h('button', { class: 'btn small primary', text: 'Retry cover upload', onclick: (e) =>
          act(e.currentTarget, () => api(`${path}/youtube/cover`, { method: 'POST' }), { done: 'Cover set.' }) })
        : null);
  }
  if (clip.review_state !== 'approved') {
    return h('div', { class: 'platform' }, name, pill('Not posted'),
      h('span', { class: 'platform-detail muted', text: 'Waiting for approval.' }));
  }
  if (platform === 'instagram' && clip.instagram_needs_manual) {
    const id = `manual-${clip.n}`;
    const url = h('input', { type: 'url', id, placeholder: 'https://www.instagram.com/reel/...' });
    return h('div', { class: 'platform' }, name, pill('Post by hand', 'warn'),
      h('span', { class: 'platform-detail', text: `${Math.round(clip.seconds)}s is over the API limit of 65s. Upload it from the Instagram app, then record the link here.` }),
      clip.video_url ? h('a', { href: clip.video_url, download: clip.file, class: 'btn small', text: 'Download video' }) : null,
      canPublish ? h('div', { class: 'inline-form' },
        h('label', { for: id, class: 'sr' }, 'Instagram link'), url,
        h('button', { class: 'btn small primary', text: 'Mark as posted', onclick: (e) => {
          if (!/^https?:\/\//.test(url.value.trim())) { toast('Paste the post\'s link first.', 'bad'); url.focus(); return; }
          act(e.currentTarget, () => api(`${path}/instagram/manual`, { method: 'POST', body: { url: url.value.trim() } }), { done: 'Recorded.' });
        } })) : null);
  }
  return h('div', { class: 'platform' }, name, pill('Not posted'),
    h('span', { class: 'platform-detail muted', text: 'Approved; choose it above to post.' }));
}

async function followJob(jobId, area) {
  const log = h('pre', { class: 'joblog', 'aria-live': 'polite' });
  const status = pill('Posting…', 'info');
  area.replaceChildren(h('div', { class: 'card plan' }, h('div', { class: 'card-head' }, h('h2', { text: 'Posting' }), status), log));
  for (;;) {
    const job = await api(`/publish-jobs/${encodeURIComponent(jobId)}`);
    log.textContent = job.log.filter(Boolean).join('\n') || 'Starting…';
    if (job.status !== 'running') {
      const failed = job.results.filter((x) => x.error).length;
      toast(failed ? `${failed} post(s) failed; see the log.` : 'Posted.', failed ? 'bad' : '');
      status.replaceWith(pill(job.status, RUN_TONE[job.status]));
      setTimeout(() => render(), 1500);
      return;
    }
    await new Promise((res) => setTimeout(res, 1500));
  }
}

// --- One run -------------------------------------------------------------------

async function pageRun(r) {
  const id = r.args[0];
  if (!id) return go('#/history');
  const run = await api(`/runs/${encodeURIComponent(id)}`);
  const steps = h('div', {}, stepList(run, run.events));
  const live = run.status === 'running' || run.status === 'starting';
  if (live) {
    const source = new EventSource(`${API}/runs/${encodeURIComponent(id)}/events`);
    const events = [...run.events];
    source.addEventListener('step', (e) => {
      // The run has opened its record: redraw once to show who and when.
      if (run.status === 'starting') { source.close(); render(); return; }
      const ev = JSON.parse(e.data);
      if (ev.id && events.some((x) => x.id === ev.id)) return;
      events.push(ev);
      steps.replaceChildren(stepList({ ...run, status: 'running' }, events));
    });
    source.addEventListener('end', () => { source.close(); render(); });
    source.onerror = () => { /* the browser retries; the page also refreshes on 'end' */ };
    state.cleanup.push(() => source.close());
  }
  const logBox = h('pre', { class: 'joblog' }, 'Loading…');
  api(`/runs/${encodeURIComponent(id)}/log?lines=300`).then((t) => { logBox.textContent = t; })
    .catch(() => logBox.replaceWith(h('p', { class: 'muted small', text: 'No log file for this run.' })));

  return h('div', { class: 'page-body' },
    h('div', { class: 'head' },
      h('div', { class: 'head-text' }, h('span', { class: 'eyebrow', text: `Run ${run.kind || ''}` }),
        h('h1', { class: 'mono-title', text: run.id }),
        h('span', { class: 'muted', text: run.started_at
          ? `Started ${when(run.started_at)} by ${run.started_by}` +
            (run.finished_at ? ` · took ${took(run.started_at, run.finished_at)}` : '')
          : 'Starting: getting the latest code before the pipeline begins.' })),
      h('div', { class: 'head-actions' }, pill(run.status, RUN_TONE[run.status]),
        live && can('run') ? h('button', { class: 'btn', text: 'Cancel run', onclick: (e) => {
          if (!confirm('Stop this run? Clips already rendered stay.')) return;
          act(e.currentTarget, () => api(`/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }), { done: 'Cancelling…' });
        } }) : null)),
    run.error ? h('div', { class: 'card tight' }, h('p', { class: 'error', text: run.error })) : null,
    run.outcome ? h('p', { text: run.outcome }) : null,
    run.code_version ? h('p', { class: 'muted small', text: `Made by Sermon Shorts ${run.code_version}` +
      (run.code_commit ? ` (${run.code_commit})` : '') }) : null,
    run.service_id ? h('p', {}, h('a', { href: `#/week/${encodeURIComponent(run.service_id)}`, text: 'Open the service' })) : null,
    h('section', { class: 'card' }, h('h2', { text: 'Steps' }), steps),
    h('section', { class: 'card' }, h('h2', { text: 'Log' }), logBox));
}

// --- History & audit -----------------------------------------------------------

const AUDIT_FILTERS = [['All', ''], ['Approvals', 'clip.'], ['Posts', 'post.'], ['Runs', 'run.'], ['People', 'user.']];

async function pageHistory(r) {
  const filter = r.query.get('f') || '';
  const [runsResp, events] = await Promise.all([api('/runs?limit=30'), api(`/audit?limit=300${filter ? `&action=${encodeURIComponent(filter)}` : ''}`)]);
  const verifyOut = h('span', { class: 'small' });

  const runRows = runsResp.runs.map((run) => h('tr', {},
    h('td', {}, h('a', { href: `#/runs/${encodeURIComponent(run.id)}`, class: 'mono', text: run.id })),
    h('td', { class: 'nowrap', text: when(run.started_at) }),
    h('td', { text: run.github ? `${run.started_by} (GitHub)` : run.started_by || '-' }),
    h('td', {}, pill(run.status, RUN_TONE[run.status])),
    h('td', { class: 'mono', text: took(run.started_at, run.finished_at) || '-' }),
    h('td', { text: run.error || run.outcome || '' }),
    h('td', { class: 'mono nowrap', text: run.code_version || '-' })));
  const ghRows = runsResp.github_only.map((g) => h('tr', {},
    h('td', {}, h('a', { href: g.url, target: '_blank', rel: 'noopener noreferrer', class: 'mono', text: `GitHub ${g.github_run_id}` })),
    h('td', { text: when(g.created_at) }), h('td', { text: g.event === 'schedule' ? 'schedule' : g.actor || '-' }),
    h('td', {}, pill(g.conclusion || g.status, g.conclusion === 'failure' ? 'bad' : '')),
    h('td', { text: '-' }), h('td', { text: g.conclusion === 'failure' ? 'Failed before reaching the sprite' : 'Never reached the sprite' }),
    h('td', { text: '-' })));

  return h('div', { class: 'page-body' },
    h('div', { class: 'head-text' }, h('h1', { text: 'History and audit log' }),
      h('span', { class: 'muted', text: 'Every run, decision and post, with who did it and when.' })),
    h('section', { class: 'card' }, h('h2', { text: 'Runs' }),
      runRows.length || ghRows.length ? h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Run', 'Started', 'By', 'Result', 'Took', 'Outcome', 'Version'].map((t) => h('th', { scope: 'col', text: t })))),
        h('tbody', {}, runRows, ghRows))) : h('p', { class: 'muted', text: 'No runs recorded yet.' })),
    h('section', { class: 'card' },
      h('div', { class: 'card-head' },
        h('div', { class: 'head-text' }, h('h2', { text: 'Audit log' }),
          h('span', { class: 'muted small', text: 'Append-only and hash-chained: any edit or removal shows up when verified.' })),
        h('div', { class: 'actions' },
          h('button', { class: 'btn small', text: 'Verify', onclick: (e) => act(e.currentTarget, async () => {
            const v = await api('/audit/verify');
            verifyOut.replaceChildren(v.ok ? pill(`Intact · ${v.events} events`, 'ok') : pill(`Broken at event ${v.first_bad}: ${v.problem}`, 'bad'),
              v.ok ? h('span', { class: 'mono muted', text: ` tail ${v.tail.slice(0, 16)}…` }) : null);
          }, { refresh: false }) }),
          h('a', { class: 'btn small', href: `${API}/audit.csv${filter ? `?action=${encodeURIComponent(filter)}` : ''}`, download: 'sermon-shorts-audit.csv', text: 'Export CSV' }))),
      verifyOut,
      h('div', { class: 'filters', role: 'group', 'aria-label': 'Filter entries' },
        AUDIT_FILTERS.map(([label, prefix]) => h('button', { 'aria-pressed': String(prefix === filter), text: label,
          onclick: () => go(prefix ? `#/history?f=${encodeURIComponent(prefix)}` : '#/history') }))),
      events.length ? h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['When', 'Who', 'What', 'Where', 'Detail'].map((t) => h('th', { scope: 'col', text: t })))),
        h('tbody', {}, events.map((e) => h('tr', {},
          h('td', { class: 'nowrap small', text: when(e.at) }), h('td', { text: e.actor }),
          h('td', {}, pill(auditLabel(e.action), e.action.endsWith('failed') ? 'bad' : e.action.startsWith('post.') ? 'info' : e.action === 'clip.approved' ? 'ok' : '')),
          h('td', { class: 'small', text: [e.service, e.clip].filter(Boolean).join(' / ') }),
          h('td', { class: 'small', text: auditDetail(e) }))))))
        : h('p', { class: 'muted', text: 'No entries.' })));
}

// --- People --------------------------------------------------------------------

async function pagePeople() {
  const people = await api('/users');
  const owner = can('manage_people');
  const invites = owner ? await api('/invites') : [];
  const result = h('div', {});

  const role = h('select', { id: 'invite-role' }, ['reviewer', 'approver', 'owner'].map((r) => h('option', { value: r, text: r })));
  const note = h('input', { type: 'text', id: 'invite-note', placeholder: 'Who it is for, e.g. Sarah, comms team' });
  const inviteForm = owner ? h('section', { class: 'card', 'aria-labelledby': 'invite-h' },
    h('h2', { id: 'invite-h', text: 'Invite someone' }),
    h('p', { class: 'muted small', text: 'Reviewers watch and leave notes. Approvers also approve, reject and post. Owners also start runs and manage people.' }),
    h('div', { class: 'row' },
      h('div', { class: 'field side' }, h('label', { for: 'invite-role', text: 'Role' }), role),
      h('div', { class: 'field grow' }, h('label', { for: 'invite-note', text: 'Note' }), note)),
    h('div', { class: 'actions' }, h('button', { class: 'btn primary', text: 'Create invite', onclick: (e) => act(e.currentTarget, async () => {
      const inv = await api('/invites', { method: 'POST', body: { role: role.value, note: note.value } });
      const link = `${location.origin}/app/#/join?code=${encodeURIComponent(inv.code)}`;
      result.replaceChildren(h('div', { class: 'panel' },
        h('h3', { text: `Invite for a ${inv.role}` }),
        h('p', { class: 'small', text: 'Send this link privately. It works once, for 7 days, and is not shown again.' }),
        h('p', { class: 'secret', text: link }),
        h('div', { class: 'actions' }, h('button', { class: 'btn small', text: 'Copy link', onclick: () =>
          navigator.clipboard.writeText(link).then(() => toast('Copied.'), () => toast('Select the link and copy it.', 'bad')) }))));
    }, { refresh: false }) })),
    result) : null;

  const rows = people.map((p) => {
    const select = owner && !p.is_root ? h('select', { 'aria-label': `Role for ${p.name}`, onchange: (e) =>
      act(null, () => api(`/users/${encodeURIComponent(p.login)}`, { method: 'PATCH', body: { role: e.target.value } }), { done: 'Role changed.' }) },
    ['reviewer', 'approver', 'owner'].map((r) => h('option', { value: r, text: r, selected: r === p.role ? 'selected' : null }))) : null;
    return h('tr', {},
      h('td', {}, h('strong', { text: p.name }), h('br'), h('span', { class: 'mono muted', text: p.login })),
      h('td', {}, select || pill(p.role + (p.is_root ? ' (root)' : ''))),
      h('td', { text: when(p.created_at) }),
      h('td', {}, owner && !p.is_root && p.login !== state.me.login ? h('button', { class: 'btn small', text: 'Remove', onclick: (e) => {
        if (!confirm(`Remove ${p.name}? Their tokens stop working; their history stays in the log.`)) return;
        act(e.currentTarget, () => api(`/users/${encodeURIComponent(p.login)}`, { method: 'DELETE' }), { done: 'Removed.' });
      } }) : null));
  });

  return h('div', { class: 'page-body' },
    h('div', { class: 'head-text' }, h('h1', { text: 'People' }),
      h('span', { class: 'muted', text: 'Roles escalate: reviewer, approver, owner. The root owner is always an owner.' })),
    h('section', { class: 'card' }, h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Person', 'Role', 'Joined', ''].map((t) => h('th', { scope: 'col', text: t })))),
      h('tbody', {}, rows)))),
    inviteForm,
    owner && invites.length ? h('section', { class: 'card' }, h('h2', { text: 'Open invites' }),
      h('ul', { class: 'list' }, invites.map((i) => h('li', { class: 'card-head' },
        h('span', {}, pill(i.role), ` ${i.note || ''} `, h('span', { class: 'muted small', text: `by ${i.created_by_login}, expires ${when(i.expires_at)}` })),
        h('button', { class: 'btn small', text: 'Withdraw', onclick: (e) =>
          act(e.currentTarget, () => api(`/invites/${encodeURIComponent(i.id)}`, { method: 'DELETE' }), { done: 'Withdrawn.' }) })))))
      : null);
}

// --- Signing in and joining ----------------------------------------------------

async function pageSignIn() {
  const token = h('input', { type: 'password', id: 'token', autocomplete: 'current-password', placeholder: 'ssk_...' });
  const err = h('p', { class: 'error', role: 'alert' });
  const submit = async (e) => {
    e.preventDefault();
    err.textContent = '';
    try {
      state.me = await api('/session', { method: 'POST', body: { token: token.value.trim() }, allow401: true });
      go('#/');
    } catch (ex) { err.textContent = ex.message; }
  };
  return h('div', { class: 'narrow' }, h('form', { class: 'card', onsubmit: submit },
    h('h1', { text: 'Sign in' }),
    h('p', { class: 'muted', text: 'Paste the API token you got when your account was made. An owner can issue a new one if it\'s lost.' }),
    h('div', { class: 'field' }, h('label', { for: 'token', text: 'API token' }), token), err,
    h('button', { class: 'btn primary', type: 'submit', text: 'Sign in' })));
}

async function pageJoin(r) {
  const code = h('input', { type: 'text', id: 'code', value: r.query.get('code') || '', autocomplete: 'off' });
  const login = h('input', { type: 'text', id: 'login', autocomplete: 'username', placeholder: 'e.g. sarah' });
  const name = h('input', { type: 'text', id: 'name', autocomplete: 'name', placeholder: 'e.g. Sarah K.' });
  const err = h('p', { class: 'error', role: 'alert' });
  const box = h('div', { class: 'narrow' });
  const submit = async (e) => {
    e.preventDefault();
    err.textContent = '';
    try {
      const res = await api('/invites/accept', { method: 'POST', allow401: true,
        body: { code: code.value.trim(), login: login.value.trim(), name: name.value.trim() } });
      state.me = res;
      history.replaceState(null, '', '#/join');  // the code is used; take it out of the address bar
      box.replaceChildren(h('div', { class: 'card' }, h('h1', { text: `Welcome, ${res.name}` }),
        h('p', { text: `You're a${/^[aeiou]/.test(res.role) ? 'n' : ''} ${res.role}, and signed in on this browser.` }),
        h('p', { class: 'small', text: 'Your API token, for other browsers and the command line. It is shown once: keep it in a password manager.' }),
        h('p', { class: 'secret', text: res.token }),
        h('div', { class: 'actions' }, h('button', { class: 'btn small', text: 'Copy token', onclick: () =>
          navigator.clipboard.writeText(res.token).then(() => toast('Copied.'), () => toast('Select the token and copy it.', 'bad')) }),
        h('a', { class: 'btn primary', href: '#/', text: 'Continue' }))));
      renderTopbar('week');
    } catch (ex) { err.textContent = ex.message; }
  };
  box.append(h('form', { class: 'card', onsubmit: submit },
    h('h1', { text: 'Join the Studio' }),
    h('p', { class: 'muted', text: 'Pick a login and the name others will see on your approvals and notes.' }),
    h('div', { class: 'field' }, h('label', { for: 'code', text: 'Invite code' }), code),
    h('div', { class: 'field' }, h('label', { for: 'login', text: 'Login (lowercase, no spaces)' }), login),
    h('div', { class: 'field' }, h('label', { for: 'name', text: 'Your name' }), name), err,
    h('button', { class: 'btn primary', type: 'submit', text: 'Create my account' })));
  return box;
}

// --- start ---------------------------------------------------------------------

window.addEventListener('hashchange', () => { render(); document.getElementById('main').focus({ preventScroll: true }); });
render();
