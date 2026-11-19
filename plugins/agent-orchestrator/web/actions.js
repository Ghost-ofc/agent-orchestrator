'use strict';
/*
 * Panel actions: cancel, review, create plan, launch and retry.
 *
 * The browser only collects and sanity-checks input. Executor approval, task contracts, attempt
 * limits, path scopes and review evidence are enforced by the engine behind the API, and its
 * rejection text is shown verbatim so nothing is hidden or reinterpreted.
 */
window.AOActions = ctx => {
  const { state, $, node, t, tn, st, api, reconcile, dot, tone, agentName, FAILURE } = ctx;
  const PLACEHOLDER = '<<completar>>';
  // These headings are engine identifiers (the contract validator matches them), so they stay in English.
  const SECTIONS = [['Objective', 'objective'], ['Why', 'why'], ['Scope', 'scope'], ['Files to inspect', 'files'], ['Implementation guidance', 'guidance'],
    ['Constraints', 'constraints'], ['Acceptance criteria', 'acceptance'], ['Validation', 'validation'], ['Final report', 'report']];
  const REVIEWABLE = new Set(['awaiting_review', 'repair_required', 'rejected']);
  const RETRYABLE = new Set(['rejected', 'repair_required', 'cancelled']);
  let catalog = null;
  let launching = null;

  const lines = value => String(value || '').split('\n').map(line => line.trim()).filter(Boolean);
  const form = id => $(`form-${id}`);
  const dialog = id => $(`dlg-${id}`);
  const fieldLabel = name => { const key = `field.${name}`; const label = t(key); return label === key ? name : label; };
  const projectPath = suffix => `/api/projects/${state.project}${suffix}`;

  function showError(target, error) {
    const box = target.querySelector('.form-error'); box.replaceChildren();
    if (!error) return;
    box.append(node('p', error.message));
    if (error.detail) box.append(node('pre', error.detail, 'engine-detail'));
    box.scrollIntoView({ block: 'nearest' });
  }
  class FormError extends Error {}
  const problem = (key, vars) => new FormError(t(key, vars));

  // One submit pipeline for every dialog: lock, run, show the engine's answer, refresh.
  function wire(id, run) {
    const element = form(id);
    element.addEventListener('submit', async event => {
      event.preventDefault();
      const submit = element.querySelector('button[type=submit]');
      showError(element, null); submit.disabled = true;
      try {
        await run(element);
        dialog(id).close();
        await ctx.refresh();
        // After creating a plan or launching a job, go straight to it.
        if (state.pendingPlan) { const plan = state.pendingPlan; state.pendingPlan = null; ctx.selectPlan(plan); }
        if (state.pendingJob) { const job = state.pendingJob; state.pendingJob = null; ctx.selectAgent(job); }
      } catch (error) {
        showError(element, error);
      } finally { submit.disabled = false; }
    });
  }
  function open(id, prepare) {
    const element = form(id); element.reset(); showError(element, null);
    element.querySelector('button[type=submit]').disabled = false;
    prepare?.(element);
    dialog(id).showModal();
  }

  // ---------- Job actions ----------
  function renderJobActions(job) {
    const bar = $('actions');
    const items = [];
    if (job?.active) items.push({ key: 'cancel', label: t('actions.cancel'), className: 'danger' });
    if (job && REVIEWABLE.has(job.state)) items.push({ key: 'review', label: t('actions.review'), className: 'primary' });
    if (job?.plan_id && job.checklist_item && (FAILURE.has(job.state) || RETRYABLE.has(job.state))) items.push({ key: 'retry', label: t('actions.retry'), className: '' });
    bar.hidden = items.length === 0;
    reconcile(bar, items, item => item.key, item => {
      const button = node('button'); button.type = 'button';
      button.addEventListener('click', () => ({ cancel: openCancel, review: openReview, retry: openRetry })[item.key]());
      return button;
    }, (button, item) => { button.textContent = item.label; button.className = item.className; });
  }
  function openCancel() { open('cancel'); }
  function openReview() { open('review'); }
  wire('cancel', async element => {
    await api(projectPath(`/jobs/${state.agent}/cancel`), { force: element.elements.force.checked });
  });
  wire('review', async element => {
    const verdict = element.elements.verdict.value;
    const tests = lines(element.elements.tests.value);
    const notes = element.elements.notes.value.trim();
    if (!verdict) throw problem('review.needVerdict');
    if (verdict === 'accepted' && !tests.length) throw problem('review.needTests');
    if (!notes) throw problem('review.needNotes');
    await api(projectPath(`/jobs/${state.agent}/review`), { verdict, tests, notes });
  });

  // ---------- Plans ----------
  function planCounts(plan) {
    const counts = plan.counts || {}; const total = Object.values(counts).reduce((sum, value) => sum + value, 0);
    return { done: counts.done || 0, total };
  }
  function renderProjectActions(project) { $('new-plan').hidden = !project; }
  function renderPlansList(project) {
    const section = $('plans-list');
    const plans = project?.plans || [];
    section.hidden = !project;
    if (!project) return;
    let head = section.querySelector('.plans-head');
    if (!head) {
      head = node('div', null, 'plans-head'); head.append(node('h3', t('plan.listTitle')));
      const create = node('button', t('plan.new')); create.type = 'button'; create.addEventListener('click', openPlan); head.append(create);
      section.append(head, node('ul', null, 'plan-rows'), node('p', t('plan.none'), 'muted none'));
    }
    section.querySelector('.none').hidden = plans.length > 0;
    reconcile(section.querySelector('.plan-rows'), plans, plan => plan.plan_id, () => {
      const li = node('li'); const button = node('button', null, 'plan-row'); button.type = 'button';
      button.append(node('strong'), node('small'));
      button.addEventListener('click', () => ctx.selectPlan(li.dataset.key)); li.append(button); return li;
    }, (li, plan) => {
      const { done, total } = planCounts(plan);
      li.querySelector('strong').textContent = plan.title || plan.plan_id;
      li.querySelector('small').textContent = plan.complete ? t('canvas.planDone') : total ? t('canvas.planProgress', { done, total }) : t('canvas.planNoItems');
    });
  }
  function renderPlanDetail(plan) {
    const section = $('plan-detail');
    if (!plan) { section.replaceChildren(); return; }
    if (!section.querySelector('.plan-meta')) {
      const back = node('button', t('plan.back'), 'link'); back.type = 'button'; back.addEventListener('click', () => ctx.selectOrchestrator());
      section.append(back, node('p', null, 'plan-goal'), node('dl', null, 'plan-meta'), node('h4', t('plan.executors')), node('ul', null, 'plan-executors'), node('h4', t('plan.items')), node('ul', null, 'plan-items'));
    }
    section.querySelector('.plan-goal').textContent = plan.goal.objective || '';
    const meta = section.querySelector('.plan-meta'); meta.replaceChildren();
    const limit = plan.executor_policy.max_attempts_per_item;
    for (const [label, value] of [[t('plan.fieldStrategy'), plan.goal.strategy], [t('plan.fieldRisk'), plan.goal.risk], [t('plan.fieldType'), plan.goal.task_type],
      [t('plan.fieldTopology'), plan.team_policy?.topology], [t('plan.fieldParallel'), plan.team_policy?.max_parallel], [t('plan.attemptLimit'), limit ?? '—']]) meta.append(node('dt', label), node('dd', st('plan', value ?? '')));
    const allowed = section.querySelector('.plan-executors'); allowed.replaceChildren();
    if (!plan.executor_policy.allowed.length) allowed.append(node('li', t('plan.noExecutors'), 'muted'));
    plan.executor_policy.allowed.forEach(entry => allowed.append(node('li', entry.display)));
    reconcile(section.querySelector('.plan-items'), plan.items, item => item.id, () => {
      const li = node('li', null, 'plan-item');
      const head = node('div', null, 'plan-item-head'); head.append(dot('neutral'), node('strong'), node('small'));
      const launch = node('button', t('plan.launch'), 'primary'); launch.type = 'button'; launch.addEventListener('click', () => openLaunch(plan.plan_id, li.dataset.key));
      li.append(head, node('div', null, 'plan-jobs'), launch); return li;
    }, (li, item) => {
      li.querySelector('.dot').className = `dot ${item.state === 'done' ? 'green' : item.state === 'blocked' ? 'red' : item.state === 'in_progress' ? 'blue' : 'neutral'}`;
      li.querySelector('strong').textContent = item.title;
      li.querySelector('small').textContent = `${st('plan', item.state)} · ${tn('plan.attempts', item.attempts)}`;
      li.querySelector('.primary').hidden = !item.can_launch;
      li._item = item;
      reconcile(li.querySelector('.plan-jobs'), item.job_ids, id => id, id => {
        const button = node('button', id, 'job-chip'); button.type = 'button'; button.addEventListener('click', () => ctx.selectAgent(button.dataset.key)); return button;
      }, (button, id) => { button.textContent = projectJobName(id); });
    });
  }
  const projectJobName = id => { const job = ctx.selectedProject()?.jobs.find(entry => entry.job_id === id); return job ? `${agentName(job)} · ${st('state', job.state)}` : id; };

  async function loadCatalog(target) {
    if (catalog) return catalog;
    try { catalog = await api('/api/executors'); } catch (error) { showError(target, error); }
    return catalog;
  }
  function fillSelect(select, values, selected) {
    select.replaceChildren(...values.map(value => { const option = node('option', st('plan', value)); option.value = value; option.selected = value === selected; return option; }));
  }
  async function openPlan() {
    open('plan');
    const element = form('plan');
    const data = await loadCatalog(element);
    if (!data) return;
    fillSelect(element.elements.strategy, data.options.strategies, 'cost-first');
    fillSelect(element.elements.risk, data.options.risks, 'medium');
    fillSelect(element.elements.task_type, data.options.task_types, 'general');
    fillSelect(element.elements.topology, data.options.topologies, 'auto');
    const harness = $('plan-harness');
    harness.replaceChildren(...data.harnesses.filter(entry => entry.available).map(entry => { const option = node('option', entry.display_name || entry.cli); option.value = entry.cli; return option; }));
  }
  $('plan-add-executor').addEventListener('click', () => {
    const cli = $('plan-harness').value; const model = $('plan-model').value.trim();
    if (!cli) return;
    const area = $('plan-executors'); area.value = `${area.value.trim()}${area.value.trim() ? '\n' : ''}${model ? `${cli}=${model}` : cli}`;
    $('plan-model').value = '';
  });
  wire('plan', async element => {
    const value = name => element.elements[name].value.trim();
    for (const name of ['title', 'goal']) if (!value(name)) throw problem('err.required', { field: fieldLabel(name) });
    const checklist = lines(value('checklist'));
    if (!checklist.length) throw problem('err.required', { field: fieldLabel('checklist') });
    const body = { title: value('title'), goal: value('goal'), checklist, strategy: value('strategy'), risk: value('risk'), task_type: value('task_type'),
      topology: value('topology'), max_parallel: Number(value('max_parallel') || 3), executors: lines(value('executors')), expensive_executors: lines(value('expensive_executors')) };
    for (const name of ['decisions', 'constraints', 'validation', 'completion']) if (value(name)) body[name] = value(name);
    const plan = await api(projectPath('/plans'), body);
    state.pendingPlan = plan.plan_id;
  });

  // ---------- Launch and retry ----------
  const buildTemplate = title => SECTIONS.map(([heading, hint], index) => `## ${heading}\n${index === 0 ? title : `${PLACEHOLDER} ${t(`launch.hint.${hint}`)}`}`).join('\n\n');
  async function openRetry() {
    const job = state.detail;
    open('launch');
    const element = form('launch');
    try {
      const template = await api(projectPath(`/jobs/${job.job_id}/launch-template`));
      await prepareLaunch(element, job.plan_id, job.checklist_item, template);
    } catch (error) { showError(element, error); }
  }
  async function openLaunch(planId, itemId) {
    open('launch');
    const element = form('launch');
    try { await prepareLaunch(element, planId, itemId, null); } catch (error) { showError(element, error); }
  }
  async function prepareLaunch(element, planId, itemId, template) {
    const plan = await api(projectPath(`/plans/${planId}`));
    const item = plan.items.find(entry => entry.id === itemId);
    if (!item) throw problem('launch.noItem');
    launching = { planId, itemId, item };
    // A finished item or one at its attempt limit cannot be launched; say so instead of letting the
    // engine reject a form the person already filled in.
    if (!item.can_launch) { element.querySelector('button[type=submit]').disabled = true; throw problem('launch.blocked'); }
    $('dlg-launch-title').textContent = template ? t('launch.retryTitle') : t('launch.title');
    $('launch-item').textContent = `${plan.title} · ${item.title}`;
    const select = element.elements.executor;
    const untried = new Set(item.untried);
    select.replaceChildren(...item.allowed.map((entry, index) => {
      const option = node('option', `${entry.display}${untried.has(entry.display) ? t('launch.untried') : t('launch.tried')}`); option.value = String(index); return option;
    }));
    const wanted = template ? item.allowed.findIndex(entry => untried.has(entry.display)) : -1;
    const same = template ? item.allowed.findIndex(entry => entry.cli === template.cli && (entry.model || null) === (template.model || null)) : -1;
    select.value = String(Math.max(0, wanted >= 0 ? wanted : same));
    const sync = () => {
      const entry = item.allowed[Number(select.value)];
      $('launch-confirm-text').textContent = entry ? t('launch.confirm', { executor: entry.display }) : '';
      $('launch-executor-help').textContent = item.allowed.length ? '' : t('launch.noExecutors');
    };
    select.onchange = sync; sync();
    element.elements.task.value = template?.task || buildTemplate(item.title);
    element.elements.role.value = template?.role || '';
    element.elements.max_cost_usd.value = template?.max_cost_usd ?? '';
    element.elements.timeout_seconds.value = template?.timeout_seconds || 3600;
    element.elements.allow_paths.value = (template?.scope.allowed_paths || []).join('\n');
    element.elements.deny_paths.value = (template?.scope.denied_paths || []).join('\n');
  }
  wire('launch', async element => {
    if (!launching) throw problem('launch.noItem');
    const entry = launching.item.allowed[Number(element.elements.executor.value)];
    if (!entry) throw problem('launch.noExecutors');
    const task = element.elements.task.value;
    if (task.includes(PLACEHOLDER)) throw problem('launch.unfinished');
    if (!element.elements.confirm.checked) throw problem('launch.needConfirm');
    const body = { cli: entry.cli, task, confirm: true, timeout_seconds: Number(element.elements.timeout_seconds.value) || 3600,
      allow_paths: lines(element.elements.allow_paths.value), deny_paths: lines(element.elements.deny_paths.value) };
    if (entry.model) body.model = entry.model;
    if (element.elements.role.value.trim()) body.role = element.elements.role.value.trim();
    if (element.elements.max_cost_usd.value) body.max_cost_usd = Number(element.elements.max_cost_usd.value);
    if (element.elements.allow_dirty.checked) body.allow_dirty = true;
    if (element.elements.allow_concurrent_workspace.checked) body.allow_concurrent_workspace = true;
    const job = await api(projectPath(`/plans/${launching.planId}/items/${launching.itemId}/launch`), body);
    state.pendingJob = job.job_id;
  });

  for (const closer of document.querySelectorAll('dialog [data-close]')) closer.addEventListener('click', () => closer.closest('dialog').close());
  $('new-plan').addEventListener('click', openPlan);

  return { renderJobActions, renderPlanDetail, renderPlansList, renderProjectActions };
};
