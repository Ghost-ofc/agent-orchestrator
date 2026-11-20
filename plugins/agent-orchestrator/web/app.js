'use strict';
(() => {
  const { t, tn, st, number, time, dateLong, relative, raw } = window.I18N;
  const $ = id => document.getElementById(id);
  const node = (tag, text, className) => {
    const el = document.createElement(tag);
    if (text !== undefined && text !== null) el.textContent = String(text);
    if (className) el.className = className;
    return el;
  };
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const FAILURE = new Set(['failed', 'lost', 'scope_violated', 'timed_out']);
  const REVIEW = new Set(['awaiting_review', 'repair_required', 'rejected']);
  const RUNNING = new Set(['starting', 'running', 'cancelling']);
  const FILTERS = {
    all: () => true,
    attention: job => job.attention_required,
    active: job => job.active,
    review: job => REVIEW.has(job.state),
    failed: job => FAILURE.has(job.state),
    complete: job => job.state === 'accepted',
  };
  const state = { projects: [], allProjects: [], showArchived: false, pendingProject: null, project: null, agent: null, tab: 'messages', paused: false,
    filter: 'all', view: matchMedia('(max-width: 720px)').matches ? 'list' : 'canvas',
    generation: 0, refreshing: false, detail: null, transcript: null, followed: null, plan: null, planDetail: null, pendingPlan: null, pendingJob: null, feedback: [], drafts: new Map(),
    camera: { x: 32, y: 32, k: 1, fitted: null, touched: false }, world: { w: 0, h: 0 } };
  let token = new URLSearchParams(location.hash.slice(1)).get('token');
  // The launch capability stays in this tab, never in URLs sent to the server or localStorage.
  try {
    if (token) sessionStorage.setItem('orchestrator-token', token);
    else token = sessionStorage.getItem('orchestrator-token');
  } catch (_) { /* In-memory access still works when browser storage is disabled. */ }
  if (location.hash.startsWith('#token=')) history.replaceState(null, '', location.pathname);

  // Static markup is translated from the central dictionary.
  for (const el of document.querySelectorAll('[data-i18n]')) el.textContent = t(el.dataset.i18n);
  for (const el of document.querySelectorAll('[data-i18n-aria]')) el.setAttribute('aria-label', t(el.dataset.i18nAria));
  for (const el of document.querySelectorAll('[data-i18n-title]')) el.title = t(el.dataset.i18nTitle);
  for (const el of document.querySelectorAll('[data-i18n-placeholder]')) el.placeholder = t(el.dataset.i18nPlaceholder);

  // A failed request carries what the server said: a stable code, the offending field, and (for engine
  // rejections) the engine's own text, which is shown verbatim next to a Spanish explanation.
  class ApiError extends Error {
    constructor(message, info = {}) { super(message); Object.assign(this, info); }
  }
  const errorKeys = { 400: 'err.badAnswer', 401: 'err.auth', 403: 'err.boundary', 404: 'err.notFound', 409: 'err.notPending', 413: 'err.tooLarge' };
  async function api(path, body) {
    const controller = new AbortController();
    // Reads stay short so a stuck poll cannot freeze the refresh; actions may wait for the engine.
    const timeout = setTimeout(() => controller.abort(), path === '/api/folders/pick' ? 200000 : body === undefined ? 15000 : 90000);
    try {
      const response = await fetch(path, { signal: controller.signal, method: body === undefined ? 'GET' : 'POST',
        cache: 'no-store', credentials: 'omit', headers: { 'X-Orchestrator-Token': token || '',
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      let data = null;
      try { data = await response.json(); } catch (_) { /* Reported through the status code below. */ }
      if (!response.ok) {
        const code = data?.code; const field = data?.field;
        const message = code?.startsWith('project_') ? data.error : code === 'invalid_request' ? t('err.invalid', { field: t(`field.${field}`) === `field.${field}` ? String(field) : t(`field.${field}`) })
          : code === 'engine_rejected' ? t('err.engine') : code === 'engine_timeout' ? t('err.engineTimeout')
          : t(errorKeys[response.status] || (body === undefined ? 'err.request' : 'err.save'));
        throw new ApiError(message, { status: response.status, code, field, detail: code === 'engine_rejected' || (code === 'invalid_request' && ['workspace', 'name', 'mode', 'init_git'].includes(field)) ? data.error : null });
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new ApiError(t('err.timeout'));
      throw error;
    } finally { clearTimeout(timeout); }
  }
  const count = value => value === null || value === undefined ? t('common.unavailable') : number(value);
  const filesText = value => value === null || value === undefined ? t('count.filesUnknown') : tn('count.files', value);
  const when = value => value ? time(value) : '—';
  const ago = value => value ? relative(value) : t('common.noActivity');
  function elapsed(value) {
    if (value === null || value === undefined) return '—';
    const minutes = Math.floor(value / 60);
    return minutes < 1 ? t('time.lessThanMinute') : minutes < 60 ? t('time.min', { n: minutes }) : t('time.hourMin', { h: Math.floor(minutes / 60), m: minutes % 60 });
  }
  // Failures are red, anything a person can act on is amber, live work is blue, accepted work is green.
  function tone(item) {
    if (FAILURE.has(item.state)) return 'red';
    if (item.attention_required) return 'amber';
    if (item.active) return 'blue';
    return item.state === 'accepted' || item.completed ? 'green' : 'neutral';
  }
  function dot(color) { const el = node('span', null, `dot ${color}`); el.setAttribute('aria-hidden', 'true'); return el; }
  const statusLabel = job => st('state', job.state);
  const agentName = job => job.cli_agent || job.model || job.cli || job.job_id;
  function metric(title, value, detail) {
    const el = node('div', null, 'metric');
    el.append(node('div', title, 'label'), node('strong', value));
    if (detail) el.append(node('small', detail));
    return el;
  }
  // Reconcile keyed controls in place so polling preserves keyboard focus and answer text.
  function reconcile(parent, items, key, create, update) {
    const existing = new Map([...parent.children].map(el => [el.dataset.key, el]));
    let cursor = parent.firstElementChild;
    for (const item of items) {
      const id = key(item);
      const el = existing.get(id) || create(item);
      el.dataset.key = id;
      update(el, item);
      if (el !== cursor) parent.insertBefore(el, cursor);
      cursor = el.nextElementSibling;
      existing.delete(id);
    }
    for (const el of existing.values()) el.remove();
  }
  const selectedProject = () => state.projects.find(project => project.id === state.project);

  // ---------- Global summary and attention tray ----------
  function renderSummary() {
    const totals = { running: 0, waiting: 0, review: 0, failed: 0, done: 0 };
    for (const project of state.projects) {
      totals.waiting += project.question_count;
      for (const job of project.jobs) {
        if (RUNNING.has(job.state)) totals.running++;
        else if (REVIEW.has(job.state)) totals.review++;
        else if (FAILURE.has(job.state)) totals.failed++;
        else if (job.state === 'accepted') totals.done++;
      }
    }
    for (const [key, value] of Object.entries(totals)) {
      const item = $(`sum-${key}`);
      item.querySelector('strong').textContent = number(value);
      item.dataset.empty = String(value === 0);
    }
  }
  function attentionItems() {
    const items = [];
    for (const project of state.projects) {
      for (const feedback of project.pending_feedback) {
        items.push({ key: `q:${project.id}:feedback:${feedback.id}`, kind: 'question', pid: project.id, job: null,
          title: t('attention.coordinator'), text: feedback.question, project: project.name });
      }
      for (const job of project.jobs) {
        for (const question of job.pending_questions) {
          if (question.handled_by === 'orchestrator') continue;
          items.push({ key: `q:${project.id}:${job.job_id}:${question.id}`, kind: 'question', pid: project.id, job: job.job_id,
            title: agentName(job), text: question.question, project: project.name });
        }
        if (!job.supervised && (job.state === 'awaiting_review' || job.state === 'repair_required')) {
          items.push({ key: `r:${job.job_id}`, kind: 'review', pid: project.id, job: job.job_id,
            title: agentName(job), text: statusLabel(job), project: project.name });
        }
      }
    }
    return items;
  }
  function renderAttention() {
    const items = attentionItems();
    $('attention-count').textContent = items.length;
    $('attention-summary').dataset.alert = String(items.length > 0);
    $('attention-empty').hidden = items.length > 0;
    reconcile($('attention-list'), items, item => item.key, () => {
      const li = node('li'); const button = node('button', null, 'attention-item'); button.type = 'button';
      button.append(node('small', null, 'attention-kind'), node('strong'), node('span', null, 'attention-text'));
      button.addEventListener('click', () => { focusTarget(li._target.pid, li._target.job); $('attention').open = false; });
      li.append(button); return li;
    }, (li, item) => {
      li._target = item;
      li.dataset.kind = item.kind;
      li.querySelector('small').textContent = t('attention.open', { kind: t(`attention.${item.kind}`), project: item.project });
      li.querySelector('strong').textContent = item.title;
      li.querySelector('.attention-text').textContent = item.text;
    });
  }

  // A native <details> stays open on outside clicks; close it like any popover and return focus on Escape.
  document.addEventListener('click', event => { if (!event.target.closest('#attention')) $('attention').open = false; });
  $('attention').addEventListener('keydown', event => {
    if (event.key === 'Escape' && $('attention').open) { $('attention').open = false; $('attention-summary').focus(); }
  });

  // ---------- Navigation ----------
  function renderAgentNavigation(project) {
    const items = project ? [{ key: 'orchestrator', orchestrator: true }, ...project.jobs.map(job => ({ ...job, key: job.job_id }))] : [];
    $('agent-count').textContent = project ? project.jobs.length : 0;
    $('agent-navigation-empty').hidden = items.length > 0;
    $('agent-navigation-empty').textContent = project ? t('agents.none') : t('agents.select');
    reconcile($('agent-navigation'), items, item => item.key, () => {
      const button = node('button', null, 'agent-nav-button'); button.type = 'button';
      const text = node('span', null, 'agent-nav-copy'); text.append(node('strong'), node('small'));
      button.append(dot('neutral'), text, node('span', null, 'agent-nav-status'));
      button.addEventListener('click', () => button.dataset.agentId ? selectAgent(button.dataset.agentId) : selectOrchestrator());
      return button;
    }, (button, item) => {
      const selected = item.orchestrator ? state.agent === null && state.plan === null : state.agent === item.job_id;
      const questions = item.orchestrator ? project.pending_feedback.length : item.pending_questions.length;
      button.dataset.agentId = item.orchestrator ? '' : item.job_id;
      button.setAttribute('aria-current', String(selected));
      button.querySelector('.dot').className = `dot ${item.orchestrator ? questions ? 'amber' : 'neutral' : tone(item)}`;
      button.querySelector('strong').textContent = item.orchestrator ? t('agents.overview') : agentName(item);
      button.querySelector('small').textContent = item.orchestrator
        ? questions ? tn('rail.questions', questions) : t('agents.overviewIdle')
        : item.current_work || item.role || t('common.noProgress');
      button.querySelector('.agent-nav-status').textContent = item.orchestrator ? questions ? st('state', 'needs_input') : t('agents.ready') : statusLabel(item);
      button.setAttribute('aria-label', `${button.querySelector('strong').textContent}: ${button.querySelector('.agent-nav-status').textContent}. ${button.querySelector('small').textContent}`);
    });
  }

  // ---------- Canvas: project -> plans -> jobs, drawn only from real relations ----------
  const NODE = { w: 288, jobH: 124, cardH: 108, gap: 32, cols: [0, 360, 720] };
  function buildGraph(project, rows) {
    const grouped = new Map(project.plans.map(plan => [plan.plan_id, []]));
    const loose = [];
    for (const job of rows) (grouped.has(job.plan_id) ? grouped.get(job.plan_id) : loose).push(job);
    const nodes = []; const links = []; const planNodes = [];
    const place = (kind, key, column, top, h, data) => { const item = { kind, key, x: NODE.cols[column], y: top, h, ...data }; nodes.push(item); return item; };
    let y = 0;
    for (const plan of project.plans) {
      const group = grouped.get(plan.plan_id);
      const slots = Math.max(group.length, 1);
      const block = slots * NODE.jobH + (slots - 1) * NODE.gap;
      const planNode = place('plan', `plan:${plan.plan_id}`, 1, y + (block - NODE.cardH) / 2, NODE.cardH, { plan });
      group.forEach((job, index) => {
        const jobNode = place('job', `job:${job.job_id}`, 2, y + index * (NODE.jobH + NODE.gap), NODE.jobH, { job });
        links.push({ from: planNode, to: jobNode, tone: tone(job) });
      });
      planNodes.push(planNode);
      y += block + NODE.gap * 2;
    }
    const looseNodes = loose.map((job, index) => place('job', `job:${job.job_id}`, 2, y + index * (NODE.jobH + NODE.gap), NODE.jobH, { job }));
    const height = Math.max(nodes.reduce((max, item) => Math.max(max, item.y + item.h), 0), NODE.cardH);
    const root = place('project', 'project', 0, (height - NODE.cardH) / 2, NODE.cardH, { project });
    nodes.unshift(nodes.pop());
    for (const planNode of planNodes) links.push({ from: root, to: planNode, tone: planNode.plan.complete ? 'green' : 'neutral' });
    for (const looseNode of looseNodes) links.push({ from: root, to: looseNode, tone: tone(looseNode.job) });
    return { nodes, links, width: NODE.cols[2] + NODE.w, height };
  }
  function curve(a, b) {
    const x1 = a.x + NODE.w; const y1 = a.y + a.h / 2; const x2 = b.x; const y2 = b.y + b.h / 2; const dx = (x2 - x1) / 2;
    return `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`;
  }
  function createNode(item) {
    if (item.kind === 'job') {
      const card = node('button', null, 'node node-job'); card.type = 'button';
      const head = node('span', null, 'node-head'); head.append(dot('neutral'), node('strong', null, 'node-name'), node('span', null, 'node-badge'));
      card.append(head, node('span', null, 'node-work'), node('span', null, 'node-meta'));
      card.addEventListener('click', () => selectAgent(card.dataset.jobId));
      return card;
    }
    if (item.kind === 'plan') {
      const card = node('button', null, 'node node-plan'); card.type = 'button';
      card.append(node('small', t('canvas.plan'), 'node-kicker'), node('strong', null, 'node-name'), node('progress'), node('small', null, 'node-meta'));
      card.addEventListener('click', () => selectPlan(card.dataset.planId));
      return card;
    }
    const card = node('button', null, 'node node-project'); card.type = 'button';
    card.append(node('small', t('canvas.project'), 'node-kicker'), node('strong', null, 'node-name'), node('small', null, 'node-meta'));
    card.addEventListener('click', selectOrchestrator);
    return card;
  }
  function updateNode(card, item) {
    card.style.translate = `${item.x}px ${item.y}px`;
    card.style.width = `${NODE.w}px`; card.style.height = `${item.h}px`;
    if (item.kind === 'job') {
      const job = item.job;
      card.dataset.tone = tone(job); card.dataset.jobId = job.job_id;
      card.setAttribute('aria-pressed', String(job.job_id === state.agent));
      card.querySelector('.dot').className = `dot ${tone(job)}`;
      card.querySelector('.node-name').textContent = job.role || agentName(job);
      card.title = `${job.role || agentName(job)} · ${agentName(job)} · ${statusLabel(job)}`;
      card.querySelector('.node-badge').textContent = statusLabel(job);
      card.querySelector('.node-work').textContent = job.current_work || job.role || t('common.noProgress');
      card.querySelector('.node-meta').textContent = [job.model || job.cli, elapsed(job.elapsed_seconds), job.changed_files === null ? null : tn('count.files', job.changed_files)].filter(Boolean).join(' · ');
      card.setAttribute('aria-label', t('canvas.jobAria', { name: agentName(job), state: statusLabel(job), work: card.querySelector('.node-work').textContent }));
    } else if (item.kind === 'plan') {
      const counts = item.plan.counts || {};
      const total = Object.values(counts).reduce((sum, value) => sum + value, 0);
      const done = counts.done || 0;
      card.dataset.tone = item.plan.complete ? 'green' : 'neutral'; card.dataset.planId = item.plan.plan_id;
      card.setAttribute('aria-pressed', String(item.plan.plan_id === state.plan));
      card.querySelector('.node-name').textContent = item.plan.title || item.plan.plan_id;
      card.title = card.querySelector('.node-name').textContent;
      const bar = card.querySelector('progress'); bar.max = total || 1; bar.value = done;
      bar.setAttribute('aria-label', t('canvas.planProgress', { done, total }));
      card.querySelector('.node-meta').textContent = !total ? t('canvas.planNoItems') : item.plan.complete ? t('canvas.planDone') : t('canvas.planProgress', { done, total });
    } else {
      const project = item.project;
      card.dataset.tone = tone(project);
      card.setAttribute('aria-pressed', String(state.agent === null && state.plan === null));
      card.querySelector('.node-name').textContent = project.name;
      card.querySelector('.node-meta').textContent = t('canvas.projectStats', { agents: tn('count.agents', project.jobs.length), plans: tn('count.plans', project.plans.length) });
    }
  }
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  function applyCamera() {
    const { x, y, k } = state.camera;
    $('canvas-world').style.transform = `translate(${x}px, ${y}px) scale(${k})`;
  }
  function fitCamera() {
    const box = $('canvas'); const cw = box.clientWidth; const ch = box.clientHeight;
    if (!cw || !ch || !state.world.w) return;
    const pad = 32;
    const k = clamp(Math.min((cw - pad * 2) / state.world.w, (ch - pad * 2) / state.world.h), 0.5, 1.25);
    state.camera = { k, x: (cw - state.world.w * k) / 2, y: Math.max(pad, (ch - state.world.h * k) / 2), fitted: state.project, touched: false };
    applyCamera();
  }
  function zoomAt(factor, px, py) {
    const camera = state.camera; const k = clamp(camera.k * factor, 0.3, 1.6); const ratio = k / camera.k;
    camera.x = px - (px - camera.x) * ratio; camera.y = py - (py - camera.y) * ratio; camera.k = k; camera.touched = true;
    applyCamera();
  }
  function zoomCenter(factor) { const box = $('canvas'); zoomAt(factor, box.clientWidth / 2, box.clientHeight / 2); }
  function renderCanvas(project, rows) {
    // The empty message sits over the canvas only when there is nothing real to draw or the filter hides every job.
    const message = !project ? t('projects.empty') : !project.plans.length && !project.jobs.length ? t('projects.ready') : project.jobs.length && !rows.length ? t('canvas.noMatch') : '';
    $('canvas-empty').hidden = !message; $('canvas-empty').textContent = message;
    $('canvas-empty').classList.toggle('project-ready', Boolean(project));
    $('canvas').hidden = !project;
    if (!project) { $('canvas-nodes').replaceChildren(); $('canvas-links').replaceChildren(); return; }
    const graph = buildGraph(project, rows);
    // A graph that grew or shrank (new plan, filter) is refitted unless the person already moved the camera.
    const resized = state.world.w !== graph.width || state.world.h !== graph.height;
    state.world = { w: graph.width, h: graph.height };
    if (resized && !state.camera.touched) state.camera.fitted = null;
    reconcile($('canvas-nodes'), graph.nodes, item => item.key, createNode, updateNode);
    const svg = $('canvas-links');
    svg.setAttribute('width', graph.width); svg.setAttribute('height', graph.height);
    svg.replaceChildren(...graph.links.map(link => {
      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', curve(link.from, link.to)); path.setAttribute('class', `link ${link.tone}`);
      return path;
    }));
    if (state.camera.fitted !== state.project) fitCamera(); else applyCamera();
  }
  (() => {
    const canvas = $('canvas'); let drag = null;
    canvas.addEventListener('pointerdown', event => {
      if (event.button !== 0 || event.target.closest('.node')) return;
      drag = { x: event.clientX, y: event.clientY, cx: state.camera.x, cy: state.camera.y };
      canvas.setPointerCapture(event.pointerId); canvas.dataset.dragging = 'true';
    });
    canvas.addEventListener('pointermove', event => {
      if (!drag) return;
      state.camera.x = drag.cx + event.clientX - drag.x; state.camera.y = drag.cy + event.clientY - drag.y; state.camera.touched = true;
      applyCamera();
    });
    const stop = () => { drag = null; delete canvas.dataset.dragging; };
    canvas.addEventListener('pointerup', stop); canvas.addEventListener('pointercancel', stop);
    canvas.addEventListener('wheel', event => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      zoomAt(event.deltaY < 0 ? 1.1 : 1 / 1.1, event.clientX - rect.left, event.clientY - rect.top);
    }, { passive: false });
    canvas.addEventListener('keydown', event => {
      if (event.target !== canvas) return;
      const step = 48; const moves = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
      if (moves[event.key]) { event.preventDefault(); state.camera.x += moves[event.key][0]; state.camera.y += moves[event.key][1]; state.camera.touched = true; applyCamera(); }
      else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoomCenter(1.2); }
      else if (event.key === '-') { event.preventDefault(); zoomCenter(1 / 1.2); }
      else if (event.key === '0') { event.preventDefault(); fitCamera(); }
    });
    // Browsers scroll overflow:hidden boxes to reveal focus; pan the camera instead so the layout never drifts.
    canvas.addEventListener('scroll', () => { canvas.scrollLeft = 0; canvas.scrollTop = 0; });
    canvas.addEventListener('focusin', event => {
      const card = event.target.closest('.node'); if (!card) return;
      const a = card.getBoundingClientRect(); const b = canvas.getBoundingClientRect(); const margin = 16; let dx = 0; let dy = 0;
      if (a.left < b.left + margin) dx = b.left + margin - a.left; else if (a.right > b.right - margin) dx = b.right - margin - a.right;
      if (a.top < b.top + margin) dy = b.top + margin - a.top; else if (a.bottom > b.bottom - margin) dy = b.bottom - margin - a.bottom;
      if (dx || dy) { state.camera.x += dx; state.camera.y += dy; state.camera.touched = true; applyCamera(); }
    });
    $('zoom-in').addEventListener('click', () => zoomCenter(1.2));
    $('zoom-out').addEventListener('click', () => zoomCenter(1 / 1.2));
    $('zoom-fit').addEventListener('click', fitCamera);
    let resizing = 0;
    addEventListener('resize', () => {
      clearTimeout(resizing);
      resizing = setTimeout(() => { if (!state.camera.touched && state.view === 'canvas') { state.camera.fitted = null; renderOverview(); } }, 150);
    });
  })();

  // ---------- Project overview: header, health, canvas and list ----------
  function renderOverview() {
    const p = selectedProject();
    const selected = p?.jobs.find(job => job.job_id === state.agent) || null;
    const filtered = p ? p.jobs.filter(job => job.job_id === state.agent || FILTERS[state.filter](job)) : [];
    $('project-count').textContent = state.projects.length;
    $('projects-empty').hidden = state.projects.length > 0;
    renderSummary(); renderAttention();
    reconcile($('projects'), state.projects, item => item.id, item => {
      const button = node('button', null, 'project-button'); button.type = 'button';
      const title = node('div'); title.append(node('strong'), node('small'));
      button.append(dot('neutral'), title, node('span', null, 'count'));
      button.addEventListener('click', () => selectProject(item.id));
      return button;
    }, (button, item) => {
      button.setAttribute('aria-current', String(item.id === state.project));
      button.children[0].className = `dot ${tone(item)}`;
      button.querySelector('strong').textContent = item.name;
      button.classList.toggle('archived', Boolean(item.archived));
      button.querySelector('small').textContent = item.archived ? t('projects.archived') : item.question_count ? tn('rail.questions', item.question_count) : item.active ? t('rail.active', { active: tn('count.active', item.active), agents: tn('count.agents', item.jobs.length) }) : t('rail.history', { agents: tn('count.agents', item.jobs.length) });
      button.querySelector('.count').textContent = item.jobs.length;
      button.title = `${item.name} · ${item.id}`;
    });
    actions.renderProjectActions(p);
    renderAgentNavigation(p);
    $('project-title').textContent = p ? p.name : t('project.default');
    $('project-subtitle').textContent = p ? t('project.summary', { agents: tn('count.agents', p.jobs.length), plans: tn('count.plans', p.plans.length), ago: ago(p.updated_at) }) : t('project.empty');
    $('filter').disabled = state.view === 'list' && Boolean(selected);
    $('filter').value = state.filter;
    const health = $('health'); health.replaceChildren();
    if (selected) {
      const statusMetric = metric(t('health.agentStatus'), statusLabel(selected), selected.role || t('common.worker'));
      statusMetric.querySelector('strong').prepend(dot(tone(selected)));
      health.append(statusMetric, metric(t('health.phase'), raw(selected.phase || t('common.notReported')), selected.recent_message ? t('health.progressReported') : t('health.noProgress')), metric(t('health.elapsed'), elapsed(selected.elapsed_seconds), selected.active ? t('health.stillRunning') : t('health.latestRun')), metric(t('health.files'), selected.changed_files === null ? '—' : count(selected.changed_files), selected.changed_files === null ? t('summary.noBaseline') : t('health.fromBaseline')));
    } else if (p) {
      const healthMetric = metric(t('health.overall'), p.attention_required ? t('health.questionWaiting') : p.active ? t('health.inProgress') : p.completed ? t('health.historyAvailable') : t('health.noActive'), t('health.overallDetail', { active: tn('count.active', p.active), questions: tn('count.questions', p.question_count) }));
      healthMetric.querySelector('strong').prepend(dot(tone(p)));
      const total = p.jobs.length;
      const progressMetric = metric(t('health.reviewProgress'), total ? `${Math.round(p.completed / total * 100)}%` : '—', t('health.acceptedOf', { done: p.completed, total }));
      const progress = node('progress'); progress.max = total || 1; progress.value = p.completed; progress.setAttribute('aria-label', t('health.acceptedAria'));
      progressMetric.querySelector('strong').after(progress);
      health.append(healthMetric, progressMetric, metric(t('health.latestActivity'), ago(p.updated_at), t('health.localState')), metric(t('health.reviewQueue'), count(p.review_queue), tn('health.failedHistory', p.failed)));
    }
    for (const [view, panel] of [['canvas', 'canvas-panel'], ['list', 'list-panel']]) {
      $(panel).hidden = state.view !== view;
      $(`view-${view}`).setAttribute('aria-pressed', String(state.view === view));
    }
    if (state.view === 'canvas') renderCanvas(p, filtered);
    else renderList(p, selected, filtered);
  }
  function renderList(p, selected, filtered) {
    const rows = selected ? [selected] : filtered;
    $('agents-heading').textContent = selected ? t('table.selected') : t('table.heading', { n: rows.length });
    $('table-empty').hidden = rows.length > 0;
    $('table-empty').textContent = !p ? t('table.noProjects') : p.jobs.length ? t('table.noMatch') : t('table.noJobs');
    reconcile($('agents'), rows, j => j.job_id, () => {
      const row = node('tr');
      for (let i = 0; i < 6; i++) row.append(node('td'));
      const button = node('button', null, 'agent-button'); button.type = 'button';
      button.append(dot('neutral'), node('span'));
      button.addEventListener('click', () => selectAgent(row.dataset.key));
      row.children[0].append(button, node('small'));
      for (let i = 1; i < 6; i++) row.children[i].append(node('span'), node('small'));
      row.children[5].className = 'state-cell';
      return row;
    }, (row, j) => {
      row.classList.toggle('selected', j.job_id === state.agent);
      const cells = row.children;
      const button = cells[0].querySelector('button');
      button.setAttribute('aria-pressed', String(j.job_id === state.agent));
      button.querySelector('.dot').className = `dot ${tone(j)}`;
      button.querySelector('span:last-child').textContent = agentName(j);
      cells[0].querySelector('small').textContent = j.role || t('common.worker');
      const values = [[j.cli || t('common.configuredCli'), j.model || t('common.configuredModel')], [j.current_work || t('common.noProgress'), t('table.filesUsage', { files: filesText(j.changed_files), usage: j.usage ? t('table.usageReported') : t('table.usageUnavailable') })], [raw(j.phase || t('common.notReported')), j.recent_message ? t('table.workerUpdate') : t('common.awaitingUpdate')], [elapsed(j.elapsed_seconds), ''], [statusLabel(j), t('table.review', { state: st('review', j.review_state) })]];
      values.forEach(([primary, secondary], i) => { cells[i + 1].firstChild.textContent = primary; cells[i + 1].lastChild.textContent = secondary; });
      cells[5].firstChild.prepend(dot(tone(j)));
    });
  }
  function focusTarget(pid, job) {
    $('notice').textContent = '';
    state.project = pid; state.agent = job; state.plan = null;
    document.body.classList.add('inspector-open');
    setTab('messages'); clearInspector(); renderOverview(); loadDetail();
  }
  function selectProject(id) {
    if (state.project === id) return;
    $('notice').textContent = '';
    state.project = id;
    document.body.classList.remove('inspector-open');
    state.agent = null; state.plan = null;
    state.feedback = []; clearInspector(); renderOverview(); loadDetail();
  }
  function selectAgent(id) {
    if (state.agent === id && state.plan === null) { document.body.classList.add('inspector-open'); return; }
    $('notice').textContent = '';
    document.body.classList.add('inspector-open');
    state.agent = id; state.plan = null; clearInspector(); renderOverview(); loadDetail();
  }
  function selectPlan(id) {
    if (state.plan === id) { document.body.classList.add('inspector-open'); return; }
    $('notice').textContent = '';
    document.body.classList.add('inspector-open');
    state.plan = id; state.agent = null; clearInspector(); renderOverview(); loadDetail();
  }
  function selectOrchestrator() {
    document.body.classList.add('inspector-open');
    $('notice').textContent = '';
    if (state.agent !== null || state.plan !== null) { state.agent = null; state.plan = null; clearInspector(); renderOverview(); }
    setTab('messages'); loadDetail();
  }
  function clearInspector() {
    state.generation++; state.detail = null; state.planDetail = null; state.transcript = null; state.followed = null;
    ['timeline', 'questions', 'history', 'summary', 'files', 'tests', 'usage', 'stdout', 'stderr'].forEach(id => $(id).replaceChildren());
    $('detail-status').textContent = t('inspector.loading');
    $('inspector-title').textContent = state.agent ? t('inspector.loadingAgent') : state.plan ? t('plan.loading') : t('inspector.coordinator');
    $('inspector-subtitle').textContent = selectedProject()?.name || t('inspector.noProject');
  }
  function setTab(tab, focus = false) {
    state.tab = tab;
    for (const button of document.querySelectorAll('[data-tab]')) {
      const active = button.dataset.tab === tab;
      button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
      $(`panel-${button.dataset.tab}`).hidden = !active;
      if (active && focus) button.focus();
    }
  }

  // ---------- Inspector ----------
  function authorOf(event) {
    if (event.responder?.startsWith('orchestrator:')) return t('author.coordinator');
    return event.source === 'worker' ? t('author.worker') : event.source === 'orchestrator' ? t('author.coordinator') : t('author.runner');
  }
  const LONG_TEXT = 600;
  // The conversation is the runner's own events around the agent's output (stdout.log), then the
  // failure reason when the job failed. Everything is rendered as plain text.
  function conversationItems(job) {
    const transcript = state.transcript?.agent === state.agent ? state.transcript.data : null;
    const asItems = list => list.map(event => ({ key: `e:${event.id}`, kind: 'event', event }));
    let started = -1;
    job.events.forEach((event, index) => { if (event.type === 'agent_started') started = index; });
    const items = asItems(job.events.slice(0, started + 1));
    if (transcript) {
      if (transcript.dropped_messages) items.push({ key: 'note:dropped', kind: 'note', text: tn('chat.dropped', transcript.dropped_messages) });
      const finalIndex = transcript.messages.findLastIndex(message => ['text', 'result', 'error'].includes(message.kind));
      transcript.messages.forEach((message, position) => items.push({ key: `m:${message.index}`, kind: 'message', message, final: position === finalIndex }));
      if (transcript.thinking_blocks) items.push({ key: 'note:thinking', kind: 'note', text: tn('chat.thinking', transcript.thinking_blocks) });
      if (transcript.log_truncated) items.push({ key: 'note:log', kind: 'note', text: t('chat.logTruncated') });
      if (transcript.unrecognized_lines) items.push({ key: 'note:unrecognized', kind: 'note', text: tn('chat.unrecognized', transcript.unrecognized_lines) });
    }
    items.push(...asItems(job.events.slice(started + 1)));
    if (FAILURE.has(job.state) && job.logs.stderr.trim()) items.push({ key: 'failure', kind: 'failure', text: job.logs.stderr.trim() });
    return items;
  }
  function createBubble(item) {
    if (item.kind === 'note') return node('li', null, 'note');
    const li = node('li', null, 'bubble');
    const head = node('div', null, 'bubble-head'); head.append(node('strong'), node('span'), node('time'));
    li.append(head, node('div', null, 'bubble-body'));
    return li;
  }
  function setBubbleBody(li, text, clipped, open) {
    const signature = `${text.length}:${text.slice(0, 32)}:${text.slice(-32)}:${clipped}`;
    if (li._signature === signature) return;
    li._signature = signature;
    const body = li.querySelector('.bubble-body'); body.replaceChildren();
    if (text.length > LONG_TEXT) {
      const details = node('details'); details.open = Boolean(open); details.append(node('summary', `${text.slice(0, 240).replace(/\s+/g, ' ')}…`), node('p', text));
      body.append(details);
    } else body.append(node('p', text));
    if (clipped) body.append(node('small', t('chat.clipped', { n: number(20000) }), 'muted'));
  }
  const MESSAGE_AUTHOR = { text: 'author.agent', output: 'author.agent', result: 'chat.result', error: 'chat.error' };
  function updateBubble(li, item) {
    if (item.kind === 'note') { li.textContent = item.text; return; }
    const [head, label, time] = li.querySelector('.bubble-head').children;
    let source; let author; let phase = ''; let at = ''; let text; let clipped = false; let open = false;
    if (item.kind === 'event') {
      const event = item.event; source = event.source === 'worker' || event.source === 'orchestrator' ? event.source : 'runner';
      author = authorOf(event); at = when(event.at);
      // The worker-defined phase is shown verbatim; lifecycle events fall back to their translated type.
      phase = event.phase ? raw(event.phase) : '';
      text = event.message || event.question || (event.verdict ? t('event.review', { verdict: st('verdict', event.verdict) }) : event.state ? st('state', event.state) : st('event', event.type));
    } else if (item.kind === 'failure') {
      source = 'failure'; author = t('chat.failure'); text = item.text;
    } else {
      const message = item.message; source = message.kind;
      author = MESSAGE_AUTHOR[message.kind] ? t(MESSAGE_AUTHOR[message.kind]) : message.name || t('author.tool');
      text = message.text; clipped = message.clipped; open = Boolean(item.final);
    }
    li.dataset.source = source;
    head.textContent = author; label.textContent = phase; time.textContent = at;
    setBubbleBody(li, text, clipped, open);
  }
  function renderTimeline(job) {
    const list = $('timeline');
    const items = conversationItems(job);
    // Follow new output only while the reader is at the bottom (or just opened this agent).
    const follow = state.followed !== state.agent || list.scrollHeight - list.scrollTop - list.clientHeight < 32;
    if (!items.length) { list.replaceChildren(node('li', t('chat.empty'), 'empty')); return; }
    reconcile(list, items, item => item.key, createBubble, updateBubble);
    if (follow) { list.scrollTop = list.scrollHeight; state.followed = state.agent; }
  }
  function renderHistory(records) {
    const parent = $('history'); parent.replaceChildren();
    if (!records.length) return;
    parent.append(node('h3', t('history.title')));
    for (const record of [...records].reverse()) {
      const article = node('article');
      article.append(node('small', t('history.meta', { source: record.source === 'orchestrator' ? t('question.sourceCoordinator') : t('common.worker'), state: st('state', record.state), time: when(record.answered_at || record.expired_at || record.created_at) })), node('p', record.question), node('p', record.answer || t('history.closed')));
      parent.append(article);
    }
  }
  function questionRoute(q) {
    const prefix = `/api/projects/${state.project}`;
    return q.source === 'orchestrator' ? `${prefix}/feedback/${q.id}/answer` : `${prefix}/jobs/${state.agent}/questions/${q.id}/answer`;
  }
  function renderQuestions(records) {
    const parent = $('questions');
    const pendingKeys = new Set(records.map(questionRoute));
    // An answer from another tab never destroys a focused draft.
    const held = [...parent.children].filter(el => !pendingKeys.has(el.dataset.key) && el.contains(document.activeElement));
    for (const el of held) { el.querySelector('button').disabled = true; el.querySelector('.result').textContent = t('question.gone'); }
    const existing = new Map([...parent.children].map(el => [el.dataset.key, el]));
    for (const q of records) {
      const route = questionRoute(q);
      let form = existing.get(route);
      if (!form) {
        form = node('form', null, 'question'); form.dataset.key = route;
        const header = node('div', null, 'question-header'); const heading = node('h3', t('question.heading')); heading.prepend(dot('amber'));
        header.append(heading, node('time', when(q.created_at)));
        const source = `${q.source === 'orchestrator' ? t('question.sourceCoordinator') : t('question.sourceWorker', { agent: $('inspector-title').textContent })} · ${selectedProject()?.name || ''}`;
        const inputId = `answer-${q.source}-${q.id}`;
        const inputLabel = node('label', t('question.answer')); inputLabel.htmlFor = inputId;
        const input = node('textarea'); input.id = inputId; input.name = 'answer'; input.required = true; input.maxLength = 60000;
        input.value = state.drafts.get(route) || '';
        input.addEventListener('input', () => state.drafts.set(route, input.value));
        const submit = node('button', t('question.send')); submit.type = 'submit';
        const result = node('p', '', 'result'); result.setAttribute('role', 'status');
        form.append(header, node('p', source, 'source-label'), node('p', q.question, 'question-text'));
        if (q.context) form.append(node('p', q.context, 'context'));
        form.append(inputLabel, input, submit, result);
        if (q.handled_by === 'orchestrator') result.textContent = t('supervision.handling');
        form.addEventListener('submit', async event => {
          event.preventDefault();
          if (!input.value.trim()) { result.textContent = t('question.empty'); input.focus(); return; }
          const answer = input.value; submit.disabled = true; result.textContent = t('question.saving');
          try {
            await api(route, { answer }); state.drafts.delete(route);
            result.textContent = t('question.savedOk'); input.readOnly = true;
            $('notice').textContent = t('question.savedNotice');
            // Keep the successful form visible and focused until the next deliberate selection.
            form.dataset.answered = 'true'; form.querySelector('h3').textContent = t('question.saved');
            await refresh();
          } catch (error) { result.textContent = error.message; submit.disabled = false; }
        });
        parent.append(form);
      }
      if (!form.dataset.answered && !form.contains(document.activeElement)) form.querySelector('.result').textContent = q.handled_by === 'orchestrator' ? t('supervision.handling') : '';
      existing.delete(route);
    }
    for (const form of existing.values()) if (!held.includes(form) && form.dataset.answered !== 'true') form.remove();
  }
  const USAGE_ORDER = ['input_tokens', 'output_tokens', 'thinking_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'total_tokens', 'total_cost_usd', 'duration_ms'];
  const snake = key => key.replace(/([A-Z])/g, '_$1').toLowerCase();
  const money = value => new Intl.NumberFormat('es', { style: 'currency', currency: 'USD', maximumFractionDigits: 4 }).format(value);
  // Providers report the same figure as camelCase and snake_case; show each once, known fields first.
  function usageRows(usage, stats) {
    const entries = Object.entries(usage).filter(([key]) => key !== 'provider_reported');
    const keys = new Set(entries.map(([key]) => key));
    const known = new Map(); const other = [];
    for (const [key, value] of entries) {
      const canonical = snake(key);
      if (USAGE_ORDER.includes(canonical)) { if (key === canonical || !keys.has(canonical)) known.set(canonical, value); }
      else other.push([key, value]);
    }
    if (stats?.num_turns !== undefined) known.set('num_turns', stats.num_turns);
    const rows = [...known].sort(([a], [b]) => (USAGE_ORDER.indexOf(a) + 1 || 99) - (USAGE_ORDER.indexOf(b) + 1 || 99))
      .map(([key, value]) => [st('usage', key), key === 'total_cost_usd' ? money(value) : key === 'duration_ms' ? elapsed(value / 1000) : count(value)]);
    return { rows, other };
  }
  function renderFiles(job, toolFiles) {
    const panel = $('files'); panel.replaceChildren();
    if (job.files === null) panel.append(node('p', t('files.noBaseline'), 'muted'));
    else {
      panel.append(node('p', tn('files.count', job.files.length)));
      if (job.files.length) { const list = node('ul'); job.files.forEach(file => list.append(node('li', file))); panel.append(list); }
    }
    panel.append(node('h3', t('files.fromTools')));
    if (!toolFiles.length) { panel.append(node('p', t('files.noToolWrites'), 'muted')); return; }
    const list = node('ul'); toolFiles.forEach(file => list.append(node('li', `${file.path} · ${file.tool}`))); panel.append(list);
  }
  function renderTests(job) {
    const panel = $('tests'); panel.replaceChildren(node('p', t('tests.review', { state: st('review', job.review_state) })));
    const review = job.review; const evidence = review?.tests || [];
    if (review) panel.append(node('p', t('tests.recorded', { reviewer: review.reviewer, time: when(review.reviewed_at) }), 'muted'));
    if (evidence.length) { panel.append(node('h3', t('tests.evidence'))); const list = node('ul'); evidence.forEach(test => list.append(node('li', test))); panel.append(list); }
    else panel.append(node('p', t('tests.none'), 'muted'));
    if (review?.notes) panel.append(node('h3', t('tests.notes')), node('p', review.notes, 'review-notes'));
    return evidence;
  }
  function renderUsage(job, stats) {
    const panel = $('usage'); panel.replaceChildren(node('p', job.usage ? t('usage.reported') : t('usage.none'), 'muted'));
    if (!job.usage) return;
    const { rows, other } = usageRows(job.usage, stats);
    const list = node('dl'); rows.forEach(([name, value]) => list.append(node('dt', name), node('dd', value))); panel.append(list);
    if (other.length) {
      const details = node('details'); details.append(node('summary', t('usage.other', { n: other.length })));
      const extra = node('dl'); other.forEach(([key, value]) => extra.append(node('dt', key), node('dd', count(value)))); details.append(extra); panel.append(details);
    }
  }
  function renderInspector() {
    const p = selectedProject(); const j = state.detail;
    const isWorker = Boolean(state.agent);
    $('inspector-title').textContent = j ? agentName(j) : t('inspector.coordinator');
    $('inspector-title').prepend(dot(j ? tone(j) : p?.pending_feedback.length ? 'amber' : 'neutral'));
    $('inspector-subtitle').textContent = `${j?.role || (isWorker ? t('common.worker') : t('inspector.projectDecisions'))} · ${p?.name || ''}`;
    $('local-logs').hidden = !j;
    $('detail-status').textContent = '';
    // The agent sections, the plan detail, and the plan list are mutually exclusive views.
    const viewingPlan = Boolean(state.plan);
    $('agent-sections').hidden = viewingPlan;
    $('plan-detail').hidden = !viewingPlan;
    $('show-messages').hidden = viewingPlan;
    actions.renderPlansList(!isWorker && !viewingPlan ? p : null);
    actions.renderJobActions(isWorker ? j : null);
    if (viewingPlan) {
      const plan = state.planDetail;
      $('inspector-title').textContent = plan?.title || t('plan.loading');
      $('inspector-subtitle').textContent = `${t('canvas.plan')} · ${p?.name || ''}`;
      actions.renderPlanDetail(plan);
      return;
    }
    if (j) {
      const transcript = state.transcript?.agent === state.agent ? state.transcript.data : null;
      const toolFiles = transcript?.files || [];
      renderTimeline(j);
      renderFiles(j, toolFiles);
      const evidence = renderTests(j);
      renderUsage(j, transcript?.stats);
      $('stdout').textContent = j.logs.stdout || t('logs.noOutput'); $('stderr').textContent = j.logs.stderr || t('logs.noErrors');
      renderQuestions(j.questions.filter(q => q.state === 'pending').map(q => ({ ...q, source: 'worker' })));
      renderHistory(j.questions.filter(q => q.state !== 'pending'));
      const head = node('div', null, 'summary-head'); head.append(node('h3', t('summary.title')), node('small', t('summary.elapsed', { time: elapsed(j.elapsed_seconds) })));
      const grid = node('div', null, 'summary-grid');
      const usage = j.usage || {};
      const tokens = usage.total_tokens ?? (usage.input_tokens !== undefined && usage.output_tokens !== undefined ? usage.input_tokens + usage.output_tokens : null);
      const cache = (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0);
      const filesValue = j.files !== null ? count(j.files.length) : toolFiles.length ? count(toolFiles.length) : '—';
      const filesDetail = j.files !== null ? t('summary.fromBaseline') : toolFiles.length ? t('summary.fromTools') : t('summary.noBaseline');
      const costText = usage.total_cost_usd !== undefined ? money(usage.total_cost_usd) : t('summary.costUnavailable');
      grid.append(metric(t('summary.files'), filesValue, filesDetail), metric(t('summary.tests'), evidence.length || '—', evidence.length ? t('summary.reviewRecords') : t('summary.notReported')), metric(t('summary.tokens'), tokens === null ? '—' : number(tokens), cache ? t('summary.cache', { cost: costText, cache: number(cache) }) : costText));
      $('summary').replaceChildren(head, grid);
    } else {
      $('timeline').replaceChildren(node('li', p?.pending_feedback.length ? t('chat.waitingCoordinator') : t('chat.noQuestions'), 'empty'));
      for (const id of ['files', 'tests', 'usage']) $(id).replaceChildren(node('p', t('files.select'), 'muted'));
      renderQuestions(p?.pending_feedback || []); renderHistory(state.feedback);
      $('summary').replaceChildren();
    }
  }
  // The agent's own output is fetched separately and only re-sent when stdout.log changed (etag).
  async function loadTranscript(project, agent, generation) {
    const current = state.transcript?.agent === agent ? state.transcript : null;
    try {
      const data = await api(`/api/projects/${project}/jobs/${agent}/transcript${current ? `?etag=${current.etag}` : ''}`);
      if (generation !== state.generation || (data.unchanged && current)) return;
      state.transcript = { agent, etag: data.etag, data };
    } catch (_) { /* The conversation falls back to runner events; the log tails stay available. */ }
  }
  async function loadDetail() {
    const generation = ++state.generation;
    const project = state.project; const agent = state.agent; const plan = state.plan;
    if (!project) { clearInspector(); $('detail-status').textContent = t('inspector.selectProject'); return; }
    try {
      const path = plan ? `/api/projects/${project}/plans/${plan}` : `/api/projects/${project}${agent ? '/jobs/' + agent : ''}`;
      const [data] = await Promise.all([api(path), agent ? loadTranscript(project, agent, generation) : null]);
      if (generation !== state.generation) return;
      if (plan) state.planDetail = data;
      else if (agent) state.detail = data;
      else { state.detail = null; state.feedback = data.feedback_history; const p = selectedProject(); if (p) p.pending_feedback = data.pending_feedback; }
      renderInspector();
    } catch (error) { if (generation === state.generation) $('detail-status').textContent = error.message; }
  }
  function setConnection(color, key) { $('connection').replaceChildren(dot(color), document.createTextNode(t(key))); }
  function setRefreshText() {
    $('refresh-status').textContent = state.paused ? t('refresh.paused') : t('refresh.every');
    $('pause').textContent = state.paused ? t('refresh.resume') : t('refresh.pause');
  }
  async function refresh() {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const data = await api('/api/overview'); state.allProjects = data.projects;
      state.projects = data.projects.filter(project => state.showArchived || !project.archived);
      if (!selectedProject()) {
        document.body.classList.remove('inspector-open');
        state.project = state.projects[0]?.id || null;
        state.agent = null;
        clearInspector();
      } else if (state.agent && !selectedProject().jobs.some(j => j.job_id === state.agent)) {
        state.agent = null; clearInspector();
      } else if (state.plan && !selectedProject().plans.some(plan => plan.plan_id === state.plan)) {
        state.plan = null; clearInspector();
      }
      renderOverview(); await loadDetail();
      setConnection(state.paused ? 'neutral' : 'green', state.paused ? 'connection.paused' : 'connection.ok');
      setRefreshText();
      if (data.unreadable_records) $('notice').textContent = tn('notice.unreadable', data.unreadable_records);
    } catch (error) {
      $('notice').textContent = error.message;
      setConnection('amber', 'connection.lost');
    } finally { state.refreshing = false; }
  }
  function setView(view) {
    if (state.view === view) return;
    state.view = view; state.camera.fitted = null; renderOverview();
  }
  $('show-archived').addEventListener('change', event => { state.showArchived = event.target.checked; refresh(); });
  $('view-canvas').addEventListener('click', () => setView('canvas'));
  $('view-list').addEventListener('click', () => setView('list'));
  $('filter').addEventListener('change', event => { state.filter = event.target.value; renderOverview(); });
  $('pause').addEventListener('click', () => {
    state.paused = !state.paused; $('pause').setAttribute('aria-pressed', String(state.paused));
    setRefreshText(); setConnection(state.paused ? 'neutral' : 'green', state.paused ? 'connection.paused' : 'connection.ok');
    if (!state.paused) refresh();
  });
  $('close-inspector').addEventListener('click', () => { document.body.classList.remove('inspector-open'); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !document.querySelector('dialog[open]')) document.body.classList.remove('inspector-open'); });
  $('show-messages').addEventListener('click', () => setTab('messages', true));
  const tabs = [...document.querySelectorAll('[data-tab]')];
  tabs.forEach((button, index) => {
    button.addEventListener('click', () => setTab(button.dataset.tab));
    button.addEventListener('keydown', event => {
      const target = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null;
      if (target !== null) { event.preventDefault(); setTab(tabs[target].dataset.tab, true); }
    });
  });
  function updateClock() { $('clock').textContent = `${dateLong(new Date())} · ${when(new Date().toISOString())}`; }
  const actions = window.AOActions({ state, $, node, t, tn, st, api, reconcile, dot, tone, agentName, FAILURE, refresh, selectPlan, selectAgent, selectOrchestrator, selectProject, selectedProject });
  clearInspector(); setRefreshText(); setConnection('blue', 'connection.connecting');
  $('project-title').textContent = t('project.default'); $('project-subtitle').textContent = t('project.connecting');
  $('agent-navigation-empty').textContent = t('agents.select');
  updateClock(); refresh();
  setInterval(() => { updateClock(); if (!state.paused) refresh(); }, 2000);
})();
