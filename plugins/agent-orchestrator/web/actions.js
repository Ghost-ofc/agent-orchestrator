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
  let proposing = null;
  let supervising = null;
  let editing = null;
  let coordinating = null;
  let contracting = null;
  let contractTimer = null;
  const modelPickers = new Map();
  let flowProject = null;
  let managedProject = null;

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
        if (state.pendingProject) { const id = state.pendingProject; state.pendingProject = null; ctx.selectProject(id); }
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

  $('notification-settings').addEventListener('click', async () => {
    open('notifications');
    const element = form('notifications'); element.querySelector('[type=submit]').disabled = true;
    try { element.elements.mode.value = (await api('/api/notifications')).mode; element.querySelector('[type=submit]').disabled = false; }
    catch (error) { showError(element, error); }
  });
  wire('notifications', async element => { await api('/api/notifications', { mode: element.elements.mode.value }); });

  // Project lifecycle stays separate from plan/job execution.
  $('new-project').addEventListener('click', () => { pickedParent = null; pickerGeneration++; $('project-pick-folder').disabled = false; form('project').elements.mode.disabled = false; open('project'); $('project-picker-status').textContent = ''; syncProjectMode(); });
  let pickedParent = null;
  let pickerGeneration = 0;
  function syncProjectMode() {
    const creating = form('project').elements.mode.value === 'new';
    $('project-git-choice').hidden = !creating;
    $('project-folder-name-group').hidden = !creating || !pickedParent;
    $('project-pick-folder').textContent = t(creating ? 'projects.pickParent' : 'projects.pickFolder');
  }
  $('project-pick-folder').addEventListener('click', async () => {
    const element = form('project'); const mode = element.elements.mode.value;
    const generation = pickerGeneration;
    const button = $('project-pick-folder'); const submit = element.querySelector('[type=submit]');
    const workspace = element.elements.workspace;
    const initial = workspace.value.trim();
    button.disabled = true; submit.disabled = true; element.elements.mode.disabled = true;
    $('project-picker-status').textContent = t('projects.pickerWaiting');
    showError(element, null);
    try {
      const result = await api('/api/folders/pick', { mode, ...(initial ? { initial } : {}) });
      if (generation !== pickerGeneration || !dialog('project').open) return;
      if (result.cancelled) { $('project-picker-status').textContent = t('projects.pickerCancelled'); return; }
      if (!dialog('project').open) return;
      if (mode === 'existing') { workspace.value = result.path; pickedParent = null; }
      else {
        pickedParent = result.path;
        const name = element.elements.folder_name;
        if (!name.value) name.value = element.elements.name.value.trim().toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'mi-app';
        syncPickedPath();
      }
      syncProjectMode();
      $('project-picker-status').textContent = t('projects.pickerSelected');
    } catch (error) { if (generation === pickerGeneration && dialog('project').open) { showError(element, error); $('project-picker-status').textContent = ''; } }
    finally { if (generation === pickerGeneration) { button.disabled = false; submit.disabled = false; element.elements.mode.disabled = false; } }
  });
  function syncPickedPath() {
    if (!pickedParent) return;
    const name = form('project').elements.folder_name.value.trim();
    form('project').elements.workspace.value = pickedParent.replace(/\/$/, '') + '/' + name;
  }
  $('project-folder-name').addEventListener('input', syncPickedPath);
  $('project-mode').addEventListener('change', () => { pickedParent = null; syncProjectMode(); });
  wire('project', async element => {
    const name = element.elements.name.value.trim();
    const workspace = element.elements.workspace.value.trim();
    if (!name || !workspace) throw problem('projects.required');
    if (pickedParent && element.elements.mode.value === 'new' && (!element.elements.folder_name.value.trim() || /[\/\\]/.test(element.elements.folder_name.value) || ['.', '..'].includes(element.elements.folder_name.value.trim()))) throw problem('projects.badFolderName');
    const result = await api('/api/projects', { name, workspace, mode: element.elements.mode.value,
      init_git: element.elements.mode.value === 'new' && element.elements.init_git.checked });
    state.pendingProject = result.id;
  });
  $('manage-project').addEventListener('click', async () => {
    managedProject = state.project;
    open('project-manage');
    const element = form('project-manage');
    element.querySelector('[type=submit]').disabled = true;
    $('manage-project-folder').textContent = '';
    $('manage-project-action').replaceChildren();
    $('manage-project-action').disabled = true;
    $('manage-project-confirm').hidden = true;
    try {
      const project = await api(`/api/projects/${managedProject}/settings`);
      element.elements.name.value = project.name;
      $('manage-project-folder').textContent = project.workspace;
      const values = ['rename', project.archived ? 'restore' : 'archive', 'delete'];
      element.elements.action.replaceChildren(...values.map(value => { const option = node('option', t(`projects.${value}`)); option.value = value; return option; }));
      syncProjectAction();
      $('manage-project-action').disabled = false;
      element.querySelector('[type=submit]').disabled = false;
    } catch (error) { showError(element, error); }
  });
  function syncProjectAction() {
    const action = form('project-manage').elements.action.value;
    $('manage-project-help').textContent = t(`projects.${action}Help`);
    $('manage-project-confirm').hidden = action !== 'delete';
    const submit = form('project-manage').querySelector('[type=submit]');
    submit.className = action === 'delete' ? 'danger' : 'primary';
    submit.textContent = t(action === 'delete' ? 'projects.delete' : 'projects.save');
    $('manage-project-name').readOnly = action !== 'rename';
  }
  $('manage-project-action').addEventListener('change', syncProjectAction);
  wire('project-manage', async element => {
    const action = element.elements.action.value;
    const body = { action };
    if (action === 'rename') {
      body.name = element.elements.name.value.trim();
      if (!body.name) throw problem('projects.required');
    }
    if (action === 'delete') {
      if (!element.elements.confirm.checked) throw problem('projects.needDeleteConfirm');
      body.confirm = true;
    }
    await api(`/api/projects/${managedProject}/manage`, body);
  });

  let accessProject = null;
  let sshHosts = [];
  let accessReady = false;
  let sshInventory = { hosts: [], keys: [], warnings: [] };
  let agentProfiles = {};
  $('project-access-settings').addEventListener('click', async () => {
    accessProject = managedProject;
    dialog('project-manage').close();
    open('access'); accessReady = false; sshHosts = []; agentProfiles = {}; renderAccessProfiles();
    const element = form('access');
    element.querySelector('[type=submit]').disabled = true;
    $('access-resources').disabled = true;
    $('access-mcp-list').replaceChildren(); renderSshHosts();
    try {
      const data = await api(`/api/projects/${accessProject}/access`);
      element.elements.enabled.checked = data.policy.enabled;
      $('access-per-agent').checked = data.policy.per_agent || !data.policy.enabled;
      $('access-profile-fields').hidden = !$('access-per-agent').checked;
      const selected = new Set(data.policy.mcp_servers);
      $('access-mcp-list').replaceChildren(...data.mcp_catalog.map(entry => {
        const label = node('label', null, 'choice');
        const input = node('input'); input.type = 'checkbox'; input.name = 'mcp_server'; input.value = entry.name; input.checked = selected.has(entry.name);
        const text = node('span', entry.name + (entry.available ? '' : t('access.missing')));
        text.append(node('small', entry.source, 'access-mcp-source'));
        label.append(input, text); return label;
      }));
      if (!data.mcp_catalog.length) $('access-mcp-list').append(node('p', t('access.noMcp'), 'muted'));
      sshHosts = data.policy.ssh_hosts; agentProfiles = data.policy.agent_profiles || {};
      renderSshHosts(); renderSshInventory(data.ssh_inventory); renderAccessProfiles(); accessReady = true;
      $('access-resources').disabled = !data.policy.enabled;
      element.querySelector('[type=submit]').disabled = false;
    } catch (error) { showError(element, error); }
  });
  $('access-per-agent').addEventListener('change', event => { $('access-profile-fields').hidden = !event.target.checked; });
  $('access-enabled').addEventListener('change', event => { $('access-resources').disabled = !accessReady || !event.target.checked; });
  function renderSshHosts() {
    $('access-ssh-list').replaceChildren(...sshHosts.map((host, index) => {
      const li = node('li', null, 'access-host');
      const text = node('div'); text.append(node('strong', host.name), node('p', `${host.user}@${host.host}:${host.port}`), node('small', host.identity_file));
      const remove = node('button', t('access.removeHost')); remove.type = 'button';
      remove.addEventListener('click', () => { sshHosts.splice(index, 1); renderSshHosts(); renderAccessProfiles(); });
      li.append(text, remove); return li;
    }));
  }
  $('access-add-host').addEventListener('click', () => {
    const host = { name: $('access-host-name').value.trim(), host: $('access-host-address').value.trim(), user: $('access-host-user').value.trim(),
      port: Number($('access-host-port').value || 22), identity_file: $('access-host-key').value.trim() };
    if (!host.name || !host.host || !host.user || !host.identity_file || sshHosts.some(entry => entry.name === host.name)) { showError(form('access'), problem('access.invalidHost')); return; }
    sshHosts.push(host); renderSshHosts(); renderAccessProfiles(); showError(form('access'), null);
    ['access-host-name', 'access-host-address', 'access-host-user', 'access-host-key'].forEach(id => { $(id).value = ''; });
    $('access-host-port').value = '22';
  });
  function renderSshInventory(inventory) {
    sshInventory = inventory || { hosts: [], keys: [], warnings: [] };
    $('access-ssh-summary').textContent = t('access.inventorySummary', { hosts: sshInventory.hosts.length, keys: sshInventory.keys.length });
    $('access-ssh-warnings').replaceChildren(...sshInventory.warnings.map(warning => node('p', warning)));
    $('access-known-groups').replaceChildren(...(sshInventory.groups || []).map(group => { const option = node('option'); option.value = group.name; return option; }));
    renderInventoryMatches();
  }
  function renderInventoryMatches() {
    const selected = $('access-import-host').value;
    const query = $('access-ssh-search').value.trim().toLocaleLowerCase();
    const matches = value => !query || value.toLocaleLowerCase().includes(query);
    const hosts = sshInventory.hosts.filter(host => matches([host.name, host.group, host.user, host.host, t(`access.env.${host.environment || 'unknown'}`), ...host.identity_files.map(key => key.path)].join(' ')));
    const groups = new Map();
    for (const host of hosts) {
      const group = host.group || t('access.env.unknown');
      if (!groups.has(group)) { const optgroup = node('optgroup'); optgroup.label = group; groups.set(group, optgroup); }
      const option = node('option', `${host.name} · ${host.user}@${host.host}:${host.port} · ${t(`access.env.${host.environment || 'unknown'}`)}`);
      option.value = host.name; groups.get(group).append(option);
    }
    $('access-import-host').replaceChildren(...groups.values());
    if (hosts.some(host => host.name === selected)) $('access-import-host').value = selected;
    $('access-key-inventory').replaceChildren(...sshInventory.keys.filter(key => matches([key.path, key.group, ...key.hosts].join(' '))).map(key => {
      const li = node('li'); li.append(node('strong', key.group || t('access.env.unknown')), node('span', key.path), node('small', key.hosts.length ? key.hosts.join(', ') : t(key.referenced ? 'access.patternKey' : 'access.unreferencedKey')));
      if (key.shared) li.append(node('small', t('access.sharedKey')));
      if (!key.exists) li.append(node('small', t('access.missingKey'))); return li;
    }));
    syncImportedHost();
  }
  $('access-ssh-search').addEventListener('input', renderInventoryMatches);
  function syncImportedHost() {
    const host = sshInventory.hosts.find(entry => entry.name === $('access-import-host').value);
    $('access-import-key').replaceChildren(...(host?.identity_files || []).map(key => { const option = node('option', key.path + (key.exists ? '' : t('access.missingKey'))); option.value = key.path; option.disabled = !key.exists; return option; }));
    const first = host?.identity_files.find(key => key.exists); if (first) $('access-import-key').value = first.path;
    $('access-import-detail').textContent = host ? [host.group, t(`access.env.${host.environment || 'unknown'}`), ...(host.root_account ? [t('access.rootAccount')] : []), host.source, ...host.reasons].filter(Boolean).join(' · ') : t(sshInventory.hosts.length ? 'access.noMatches' : 'access.noSshConfig');
    $('access-import-ssh').disabled = !host?.eligible || !first;
    $('access-host-group').value = host?.group || '';
    $('access-host-environment').value = host?.environment || 'unknown';
    $('access-save-organization').disabled = !host;
    $('access-organization-status').textContent = '';
  }
  $('access-import-host').addEventListener('change', syncImportedHost);
  $('access-save-organization').addEventListener('click', async () => {
    const button = $('access-save-organization'); button.disabled = true;
    try {
      renderSshInventory(await api('/api/ssh/organization', { alias: $('access-import-host').value, group: $('access-host-group').value.trim(), environment: $('access-host-environment').value }));
      $('access-organization-status').textContent = t('access.organizationSaved'); showError(form('access'), null);
    } catch (error) { showError(form('access'), error); }
    finally { button.disabled = !$('access-import-host').value; }
  });
  $('access-refresh-ssh').addEventListener('click', async () => {
    $('access-refresh-ssh').disabled = true;
    try { renderSshInventory((await api(`/api/projects/${accessProject}/access`)).ssh_inventory); }
    catch (error) { showError(form('access'), error); }
    finally { $('access-refresh-ssh').disabled = false; }
  });
  $('access-import-ssh').addEventListener('click', () => {
    const host = sshInventory.hosts.find(entry => entry.name === $('access-import-host').value);
    if (!host?.eligible) return;
    for (const [id, value] of [['access-host-name', host.name], ['access-host-address', host.host], ['access-host-user', host.user], ['access-host-port', host.port], ['access-host-key', $('access-import-key').value]]) $(id).value = value;
    $('access-host-name').focus();
  });
  const projectMcpSelection = () => [...form('access').querySelectorAll('[name=mcp_server]:checked')].map(input => input.value);
  function renderAccessProfiles() {
    const mcps = projectMcpSelection(); const hosts = sshHosts.map(host => host.name);
    // Removing a project resource also removes that grant from every profile; never widen access.
    for (const grant of Object.values(agentProfiles)) { grant.mcp_servers = grant.mcp_servers.filter(name => mcps.includes(name)); grant.ssh_hosts = grant.ssh_hosts.filter(name => hosts.includes(name)); }
    $('access-profile-list').replaceChildren(...Object.entries(agentProfiles).map(([name, grant]) => {
      const li = node('li', null, 'access-host'); const text = node('div'); text.append(node('strong', name), node('small', `MCP: ${grant.mcp_servers.join(', ') || '—'} · SSH: ${grant.ssh_hosts.join(', ') || '—'}`));
      const remove = node('button', t('access.removeHost')); remove.type = 'button'; remove.addEventListener('click', () => { delete agentProfiles[name]; renderAccessProfiles(); }); li.append(text, remove); return li;
    }));
    for (const [id, values, group] of [['access-profile-mcp', mcps, 'profile_mcp'], ['access-profile-ssh', hosts, 'profile_ssh']]) {
      const selected = new Set([...$(id).querySelectorAll('input:checked')].map(input => input.value));
      $(id).replaceChildren(...values.map(value => { const label = node('label', null, 'choice'); const input = node('input'); input.type = 'checkbox'; input.name = group; input.value = value; input.checked = selected.has(value); label.append(input, node('span', value)); return label; }));
    }
  }
  $('access-mcp-list').addEventListener('change', renderAccessProfiles);
  $('access-add-profile').addEventListener('click', () => {
    const name = $('access-profile-name').value.trim();
    if (!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(name) || Object.hasOwn(agentProfiles, name)) { showError(form('access'), problem('access.invalidProfile')); return; }
    agentProfiles[name] = { mcp_servers: [...$('access-profile-mcp').querySelectorAll('input:checked')].map(input => input.value), ssh_hosts: [...$('access-profile-ssh').querySelectorAll('input:checked')].map(input => input.value) };
    $('access-profile-name').value = ''; for (const input of form('access').querySelectorAll('[name=profile_mcp], [name=profile_ssh]')) input.checked = false;
    renderAccessProfiles(); showError(form('access'), null);
  });
  function fillAccessProfile(prefix, policy, selected = '') {
    const profiles = Object.keys(policy?.agent_profiles || {}); const select = $(`${prefix}-access-profile`);
    $(`${prefix}-access-group`).hidden = !policy?.enabled;
    const empty = node('option', t(policy?.per_agent ? 'access.noProfile' : 'access.projectDefault')); empty.value = '';
    select.replaceChildren(empty, ...profiles.map(name => { const option = node('option', name); option.value = name; return option; }));
    select.value = profiles.includes(selected) ? selected : '';
    $(`${prefix}-access-help`).textContent = t(policy?.per_agent ? 'access.chooseProfileHelp' : 'access.defaultProfileHelp');
  }

  wire('access', async element => {
    if (!accessReady) throw problem('access.loading');
    if (element.elements.enabled.checked && ['access-host-name', 'access-host-address', 'access-host-user', 'access-host-key'].some(id => $(id).value.trim())) throw problem('access.unsavedHost');
    const enabled = element.elements.enabled.checked;
    if (enabled && $('access-per-agent').checked && $('access-profile-name').value.trim()) throw problem('access.unsavedProfile');
    await api(`/api/projects/${accessProject}/access`, { enabled,
      mcp_servers: enabled ? [...element.querySelectorAll('[name=mcp_server]:checked')].map(input => input.value) : [], ssh_hosts: enabled ? sshHosts : [], per_agent: enabled && $('access-per-agent').checked, agent_profiles: enabled && $('access-per-agent').checked ? agentProfiles : {} });
  });

  // ---------- Job actions ----------
  function renderJobActions(job) {
    const access = job?.access;
    $('agent-access-info').hidden = !access;
    $('agent-access-info').textContent = access ? t('access.applied', { profile: access.profile || t(access.per_agent ? 'access.noProfile' : 'access.projectDefault'), mcp: access.mcp_servers.join(', ') || '—', ssh: access.ssh_hosts.join(', ') || '—' }) : '';
    const bar = $('actions');
    const items = [];
    if (job?.has_flow) items.push({ key: 'proposal', label: t('flow.review'), className: 'primary' });
    if (job?.active) items.push({ key: 'cancel', label: t('actions.cancel'), className: 'danger' });
    if (job && REVIEWABLE.has(job.state)) items.push({ key: 'review', label: t('actions.review'), className: 'primary' });
    if (job?.plan_id && job.checklist_item && (FAILURE.has(job.state) || RETRYABLE.has(job.state))) items.push({ key: 'retry', label: t('actions.retry'), className: '' });
    if (!job?.active) items.push({ key: 'delete', label: t('actions.deleteJob'), className: 'danger' });
    bar.hidden = items.length === 0;
    reconcile(bar, items, item => item.key, item => {
      const button = node('button'); button.type = 'button';
      button.addEventListener('click', () => ({ cancel: openCancel, review: openReview, retry: openRetry, proposal: openProposal, delete: deleteJob })[item.key]());
      return button;
    }, (button, item) => { button.textContent = item.label; button.className = item.className; });
  }
  function openCancel() { open('cancel'); }
  function openReview() { open('review'); }
  async function deleteJob() {
    if (!confirm(t('actions.confirmDeleteJob'))) return;
    try {
      await api(projectPath(`/jobs/${state.agent}/delete`), { confirm: true });
      state.agent = null;
      clearInspector();
      document.body.classList.remove('inspector-open');
      await ctx.refresh();
    } catch (err) {
      showError($('actions'), err);
    }
  }
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
  function renderProjectActions(project) { $('new-plan').hidden = !project || project.archived; $('new-flow').hidden = !project || project.archived; $('manage-project').hidden = !project; }
  function renderPlansList(project) {
    const section = $('plans-list');
    const plans = project?.plans || [];
    section.hidden = !project;
    if (!project) return;
    let head = section.querySelector('.plans-head');
    if (!head) {
      head = node('div', null, 'plans-head'); head.append(node('h3', t('plan.listTitle')));
      const flow = node('button', t('coordinator.newFlow'), 'primary'); flow.type = 'button'; flow.addEventListener('click', () => $('new-flow').click()); head.append(flow);
      const create = node('button', t('coordinator.manual')); create.type = 'button'; create.addEventListener('click', openPlan); head.append(create);
      section.append(head, node('ul', null, 'plan-rows'), node('p', t('plan.none'), 'muted none'));
    }
    head.querySelectorAll('button').forEach(button => { button.disabled = Boolean(project.archived); });
    section.querySelector('.none').hidden = plans.length > 0;
    reconcile(section.querySelector('.plan-rows'), plans, plan => plan.plan_id, () => {
      const li = node('li'); const button = node('button', null, 'plan-row'); button.type = 'button';
      button.append(node('strong'), node('small'));
      button.addEventListener('click', () => ctx.selectPlan(li.dataset.key)); li.append(button); return li;
    }, (li, plan) => {
      const { done, total } = planCounts(plan);
      li.querySelector('strong').textContent = plan.title || plan.plan_id;
      li.querySelector('small').textContent = [plan.complete ? t('canvas.planDone') : total ? t('canvas.planProgress', { done, total }) : t('canvas.planNoItems'), t('coordinator.identity', { executor: plan.coordinator?.executor || t('coordinator.none') })].join(' · ');
    });
  }
  function renderPlanDetail(plan) {
    const section = $('plan-detail');
    if (!plan) { section.replaceChildren(); return; }
    if (!section.querySelector('.plan-meta')) {
      const back = node('button', t('plan.back'), 'link'); back.type = 'button'; back.addEventListener('click', () => ctx.selectOrchestrator());
      section.append(back, node('p', null, 'plan-goal'), node('dl', null, 'plan-meta'), node('h4', t('plan.executors')), node('ul', null, 'plan-executors'), node('h4', t('plan.items')), node('ul', null, 'plan-items'));
    }
    let editButton = section.querySelector('.edit-plan-button');
    if (!editButton) {
      editButton = node('button', t('planEdit.title'), 'edit-plan-button'); editButton.type = 'button';
      section.querySelector('.plan-goal').before(editButton);
    }
    editButton.disabled = !plan.can_edit;
    editButton.title = plan.can_edit ? '' : t(ctx.selectedProject()?.archived ? 'planEdit.archived' : 'planEdit.active');
    editButton.onclick = () => openPlanEdit(plan.plan_id);
    let editStatus = section.querySelector('.plan-edit-status');
    if (!editStatus) { editStatus = node('small', null, 'plan-edit-status muted'); editButton.after(editStatus); }
    editStatus.textContent = editButton.title; editStatus.hidden = Boolean(plan.can_edit);
    let deletePlanButton = section.querySelector('.delete-plan-button');
    if (!deletePlanButton) {
      deletePlanButton = node('button', t('plan.delete'), 'delete-plan-button danger'); deletePlanButton.type = 'button';
      editStatus.after(deletePlanButton);
    }
    deletePlanButton.disabled = !plan.can_edit;
    deletePlanButton.onclick = async () => {
      if (!confirm(t('plan.confirmDelete', { title: plan.title || plan.plan_id }))) return;
      deletePlanButton.disabled = true;
      try {
        await api(projectPath(`/plans/${plan.plan_id}/delete`), { confirm: true });
        state.plan = null;
        clearInspector();
        document.body.classList.remove('inspector-open');
        await ctx.refresh();
      } catch (err) {
        showError(section, err);
      } finally {
        deletePlanButton.disabled = false;
      }
    };
    let coordinatorPanel = section.querySelector('.plan-coordinator');
    if (!coordinatorPanel) { coordinatorPanel = node('div', null, 'plan-coordinator'); section.querySelector('.plan-goal').before(coordinatorPanel); }
    coordinatorPanel.replaceChildren(node('strong', t('coordinator.identity', { executor: plan.coordinator?.executor || t('coordinator.none') })));
    const choose = node('button', t(plan.coordinator?.executor ? 'coordinator.change' : 'coordinator.choose')); choose.type = 'button'; choose.disabled = !plan.can_edit;
    choose.addEventListener('click', () => openCoordinator(plan)); coordinatorPanel.append(choose);
    section.querySelector('.plan-goal').textContent = plan.goal.objective || '';
    const meta = section.querySelector('.plan-meta'); meta.replaceChildren();
    const limit = plan.executor_policy.max_attempts_per_item;
    for (const [label, value] of [[t('flow.coordinator'), plan.coordinator?.executor || t('coordinator.none')], [t('plan.fieldStrategy'), plan.goal.strategy], [t('plan.fieldRisk'), plan.goal.risk], [t('plan.fieldType'), plan.goal.task_type],
      [t('plan.fieldTopology'), plan.team_policy?.topology], [t('plan.fieldParallel'), plan.team_policy?.max_parallel], [t('plan.attemptLimit'), limit ?? '—']]) meta.append(node('dt', label), node('dd', st('plan', value ?? '')));
    let supervisionPanel = section.querySelector('.supervision-panel');
    if (!supervisionPanel) { supervisionPanel = node('section', null, 'supervision-panel'); section.append(supervisionPanel); }
    const supervision = plan.supervision || {};
    const currentSig = JSON.stringify(supervision);
    if (supervisionPanel._lastSig !== currentSig) {
      const openTasks = new Set([...supervisionPanel.querySelectorAll('details[data-task-id][open]')].map(d => d.dataset.taskId));
      const scrollPositions = new Map();
      supervisionPanel.querySelectorAll('.engine-detail').forEach((el, idx) => {
        if (el.scrollTop > 0) scrollPositions.set(idx, el.scrollTop);
      });
      supervisionPanel.replaceChildren(node('h4', t('supervision.title')));
      supervisionPanel.append(node('p', t(supervision.enabled ? 'supervision.active' : 'supervision.paused'), 'muted'));
      {
        const button = node('button', t(supervision.enabled ? 'supervision.stop' : 'supervision.start')); button.type = 'button';
        button.addEventListener('click', async () => {
          button.disabled = true;
          try {
            if (supervision.enabled) { await api(`/api/projects/${state.project}/plans/${plan.plan_id}/supervision`, { enabled: false, confirm: true }); await ctx.refresh(); }
            else await openSupervision(plan);
          } catch (error) { supervisionPanel.append(node('p', error.message, 'form-error')); }
          finally { button.disabled = false; }
        });
        supervisionPanel.append(button);
      }
      if (supervision.error) supervisionPanel.append(node('p', supervision.error, 'form-error'));
      const report = supervision.report;
      if (report) {
        supervisionPanel.append(node('p', report.summary));
        for (const item of report.tasks || []) {
          const details = node('details');
          const taskId = item.id || item.title;
          details.dataset.taskId = taskId;
          if (openTasks.has(taskId) || (report.tasks.length === 1 && item.state === 'done')) details.open = true;
          details.append(node('summary', `${item.title} · ${item.verdict ? st('state', item.verdict) : t('supervision.pending')}`));
          details.append(node('h5', t('supervision.agentReport')), node('pre', item.delivery || '—', 'engine-detail'), node('h5', t('supervision.review')), node('pre', item.review || '—', 'engine-detail'));
          if (item.tests?.length) details.append(node('h5', t('supervision.tests')), node('pre', item.tests.join('\n'), 'engine-detail'));
          supervisionPanel.append(details);
        }
        for (const decision of report.decisions || []) supervisionPanel.append(node('p', decision));
        for (const issue of report.needs_user || []) supervisionPanel.append(node('p', `${t('supervision.user')}: ${issue.reason}`, 'access-boundary'));
      }
      supervisionPanel.querySelectorAll('.engine-detail').forEach((el, idx) => {
        if (scrollPositions.has(idx)) el.scrollTop = scrollPositions.get(idx);
      });
      supervisionPanel._lastSig = currentSig;
    }
    const allowed = section.querySelector('.plan-executors'); allowed.replaceChildren();
    if (!plan.executor_policy.allowed.length) allowed.append(node('li', t('plan.noExecutors'), 'muted'));
    plan.executor_policy.allowed.forEach(entry => allowed.append(node('li', entry.display)));
    let batchButton = section.querySelector('.batch-contracts-button');
    if (!batchButton) {
      batchButton = node('button', t('contract.batchGenerate'), 'batch-contracts-button primary');
      batchButton.type = 'button';
      section.querySelector('.plan-items').before(batchButton);
    }
    const pendingEditable = plan.items.filter(i => i.editable && !i.contract_ready);
    batchButton.hidden = pendingEditable.length === 0;
    let autoRunButton = section.querySelector('.auto-run-button');
    if (!autoRunButton) {
      autoRunButton = node('button', t('plan.autoRun'), 'auto-run-button primary');
      autoRunButton.type = 'button';
      batchButton.after(autoRunButton);
    }
    const hasPendingTasks = plan.items.some(i => i.state !== 'done');
    autoRunButton.hidden = pendingEditable.length > 0 || !hasPendingTasks;
    const isAutoRunning = plan.supervision?.enabled && plan.supervision?.auto_advance;
    if (isAutoRunning) {
      autoRunButton.disabled = true;
      autoRunButton.textContent = t('plan.autoRunning');
    } else {
      autoRunButton.disabled = !plan.can_edit;
      autoRunButton.textContent = t('plan.autoRun');
    }
    autoRunButton.onclick = async () => {
      if (!confirm(t('plan.autoRunConfirm'))) return;
      autoRunButton.disabled = true;
      autoRunButton.textContent = t('plan.autoRunning');
      try {
        await api(projectPath(`/plans/${plan.plan_id}/auto-run`), { confirm: true });
        await ctx.refresh();
      } catch (err) {
        autoRunButton.disabled = false;
        autoRunButton.textContent = t('plan.autoRun');
        showError(section, err);
      }
    };
    const batchDraft = plan.batch_contract_draft;
    if (batchDraft?.state === 'running') {
      batchButton.disabled = true;
      batchButton.textContent = t('contract.batchGenerating');
      startBatchPolling(plan.plan_id, batchButton);
    } else {
      batchButton.disabled = !plan.can_edit;
      batchButton.textContent = t('contract.batchGenerate');
    }
    batchButton.onclick = async () => {
      if (!confirm(t('contract.batchConfirm', { count: pendingEditable.length, executor: plan.coordinator?.executor || t('coordinator.none') }))) return;
      batchButton.disabled = true;
      batchButton.textContent = t('contract.batchGenerating');
      try {
        await api(projectPath(`/plans/${plan.plan_id}/contracts/batch`), { confirm: true });
        startBatchPolling(plan.plan_id, batchButton);
      } catch (err) {
        batchButton.disabled = false;
        batchButton.textContent = t('contract.batchGenerate');
        showError(section, err);
      }
    };
    reconcile(section.querySelector('.plan-items'), plan.items, item => item.id, () => {
      const li = node('li', null, 'plan-item');
      const head = node('div', null, 'plan-item-head'); head.append(dot('neutral'), node('strong'), node('small'));
      const launch = node('button', t('plan.launch'), 'primary'); launch.type = 'button'; launch.addEventListener('click', () => openLaunch(plan.plan_id, li.dataset.key));
      const generate = node('button', t('contract.generate'), 'generate-contract'); generate.type = 'button';
      li.append(head, node('p', null, 'plan-assignment muted'), node('p', null, 'plan-dependencies muted'), node('div', null, 'plan-jobs'), generate, launch); return li;
    }, (li, item) => {
      li.querySelector('.dot').className = `dot ${item.state === 'done' ? 'green' : item.state === 'blocked' ? 'red' : item.state === 'in_progress' ? 'blue' : 'neutral'}`;
      li.querySelector('strong').textContent = item.title;
      li.querySelector('small').textContent = `${st('plan', item.state)} · ${tn('plan.attempts', item.attempts)}`;
      const launchBtn = li.querySelector('.primary');
      launchBtn.hidden = item.state === 'done';
      launchBtn.textContent = t(item.contract_ready || !item.editable ? 'plan.launch' : 'contract.prepare');
      if (!item.dependencies_met && item.state !== 'done') {
        launchBtn.disabled = true;
        launchBtn.title = t('plan.waitingDeps', { deps: (item.dependency_titles || []).join(', ') });
      } else {
        launchBtn.disabled = !item.can_launch;
        launchBtn.title = '';
      }
      const generate = li.querySelector('.generate-contract'); generate.hidden = !item.editable; generate.disabled = !plan.can_edit && !item.contract_draft;
      generate.textContent = t(item.contract_draft?.state === 'ready' ? 'contract.viewDraft' : 'contract.generate');
      generate.onclick = () => openContract(plan, item);
      li.querySelector('.plan-assignment').textContent = item.assigned_executor || '';
      li.querySelector('.plan-assignment').hidden = !item.assigned_executor;
      const depsEl = li.querySelector('.plan-dependencies');
      if (item.depends_on && item.depends_on.length) {
        depsEl.hidden = false;
        depsEl.textContent = t('plan.dependsOn', { deps: (item.dependency_titles || []).join(', ') });
      } else {
        depsEl.hidden = true;
      }
      li._item = item;
      reconcile(li.querySelector('.plan-jobs'), item.job_ids, id => id, id => {
        const button = node('button', id, 'job-chip'); button.type = 'button'; button.addEventListener('click', () => ctx.selectAgent(button.dataset.key)); return button;
      }, (button, id) => { button.textContent = projectJobName(id); });
    });
  }
  let batchPollTimer = null;
  function startBatchPolling(planId, button) {
    if (batchPollTimer) clearTimeout(batchPollTimer);
    const check = async () => {
      try {
        const draft = await api(projectPath(`/plans/${planId}/contracts/batch`));
        if (draft.state === 'running') {
          batchPollTimer = setTimeout(check, 2500);
          return;
        }
        if (draft.state === 'ready') {
          if (button) button.textContent = t('contract.batchReady');
          await api(projectPath(`/plans/${planId}/contracts/batch/accept`), { confirm: true, job_id: draft.job_id });
          await ctx.refresh();
          return;
        }
        if (draft.state === 'failed' || draft.state === 'invalid') {
          alert(draft.error || 'Error al generar los contratos.');
          await ctx.refresh();
          return;
        }
      } catch (err) {
        console.error('Batch contract poll error:', err);
        await ctx.refresh();
      }
    };
    batchPollTimer = setTimeout(check, 2000);
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
  function modelElements(prefix) {
    return { select: $(prefix === 'plan' ? 'plan-harness' : `${prefix}-cli`), input: $(`${prefix}-model`), target: prefix === 'plan' ? $('plan-add-executor') : form(prefix).querySelector('[type=submit]') };
  }
  function updateModelGate(prefix) {
    const picker = modelPickers.get(prefix); if (!picker) return;
    const { select, target } = modelElements(prefix);
    picker.blocked = Boolean(picker.policy?.enabled && !picker.harnesses.find(entry => entry.cli === select.value)?.restricted_supported);
    target.disabled = picker.loading || picker.blocked || !select.value;
    picker.note.textContent = t(picker.blocked ? 'models.blocked' : picker.policy?.enabled ? 'models.strictInfo' : 'models.nativeInfo');
    picker.access.hidden = !picker.policy?.enabled;
  }
  function renderModelChoices(prefix) {
    const picker = modelPickers.get(prefix); const { input } = modelElements(prefix);
    const onlyFree = !picker.freeLabel.hidden && picker.free.checked;
    const rows = picker.models.filter(model => !onlyFree || model.free);
    const empty = node('option', t('models.choose')); empty.value = '';
    picker.choice.replaceChildren(empty, ...rows.map(model => { const option = node('option', [model.name, model.free && !/\bfree\b/i.test(model.name) ? 'Free' : '', model.name !== model.id ? model.id : ''].filter(Boolean).join(' · ')); option.value = model.id; return option; }));
    picker.choice.value = rows.some(model => model.id === input.value) ? input.value : '';
    picker.list.replaceChildren(...rows.map(model => { const option = node('option', model.name); option.value = model.id; return option; }));
  }
  async function discoverModels(prefix, refresh = false) {
    const picker = modelPickers.get(prefix); const { select, input } = modelElements(prefix);
    const cli = select.value; const ticket = ++picker.ticket; picker.loading = true; updateModelGate(prefix);
    picker.status.textContent = t('models.loading'); picker.refresh.disabled = true;
    try {
      const data = await api(`/api/executors/${cli}/models${refresh ? '/refresh' : ''}`);
      if (modelPickers.get(prefix) !== picker || ticket !== picker.ticket || select.value !== cli) return;
      picker.models = data.models || []; picker.freeLabel.hidden = cli !== 'opencode';
      if (cli === 'opencode') picker.free.checked = !input.value || picker.models.some(model => model.id === input.value && model.free);
      renderModelChoices(prefix); picker.status.textContent = data.warning || t('models.loaded', { n: picker.models.length });
    } catch (error) {
      if (modelPickers.get(prefix) !== picker || ticket !== picker.ticket) return;
      picker.models = (picker.harnesses.find(entry => entry.cli === cli)?.configured_models || []).map(id => ({ id, name: id, free: id.endsWith('-free') }));
      picker.freeLabel.hidden = cli !== 'opencode'; renderModelChoices(prefix); picker.status.textContent = t('models.fallback');
    } finally {
      if (modelPickers.get(prefix) === picker && ticket === picker.ticket) { picker.loading = false; picker.refresh.disabled = false; updateModelGate(prefix); }
    }
  }
  async function setupHarnessModels(prefix, harnesses, policy, selected = '') {
    const { select, input } = modelElements(prefix);
    const installed = harnesses.filter(entry => entry.available);
    select.replaceChildren(...installed.map(entry => { const option = node('option', (entry.display_name || entry.cli) + (policy?.enabled && !entry.restricted_supported ? ` · ${t('models.nativeOnly')}` : '')); option.value = entry.cli; return option; }));
    const [cli, ...model] = selected.split('=');
    if (installed.some(entry => entry.cli === cli)) select.value = cli;
    else if (policy?.enabled) select.value = installed.find(entry => entry.restricted_supported)?.cli || installed[0]?.cli || '';
    input.value = select.value === cli ? model.join('=') : '';
    let container = $(`${prefix}-model-picker`);
    if (!container) { container = node('div', null, 'model-picker'); container.id = `${prefix}-model-picker`; if (prefix === 'plan') input.closest('.add-executor').after(container); else input.after(container); }
    const label = node('label', t('models.available')); label.htmlFor = `${prefix}-model-choice`;
    const choice = node('select'); choice.id = label.htmlFor;
    const freeLabel = node('label', null, 'choice'); const free = node('input'); free.type = 'checkbox'; free.checked = true; freeLabel.append(free, node('span', t('models.onlyFree'))); freeLabel.hidden = select.value !== 'opencode';
    const refresh = node('button', t('models.refresh')); refresh.type = 'button';
    const status = node('small', '', 'muted'); status.setAttribute('role', 'status');
    const note = node('p', '', 'muted'); const access = node('button', t('models.access')); access.type = 'button';
    let list = $(`${prefix}-models`); if (!list) { list = node('datalist'); list.id = `${prefix}-models`; container.after(list); }
    input.setAttribute('list', list.id);
    container.replaceChildren(label, choice, freeLabel, refresh, status, note, access);
    const picker = { project: state.project, harnesses: installed, policy, models: [], ticket: 0, choice, freeLabel, free, refresh, status, note, access, list, loading: false };
    modelPickers.set(prefix, picker);
    choice.onchange = () => { if (choice.value) input.value = choice.value; };
    free.onchange = () => { if (free.checked && !picker.models.some(model => model.id === input.value && model.free)) input.value = ''; renderModelChoices(prefix); };
    refresh.onclick = () => discoverModels(prefix, true);
    access.onclick = () => { managedProject = picker.project; $('project-access-settings').click(); };
    select.onchange = () => { input.value = ''; discoverModels(prefix); };
    if (!installed.length) { updateModelGate(prefix); showError(form(prefix), problem('flow.noHarness')); return; }
    await discoverModels(prefix);
  }
  function selectedModel(prefix) {
    const picker = modelPickers.get(prefix); const { select, input } = modelElements(prefix); const model = input.value.trim();
    if (picker?.blocked) throw problem('models.blocked');
    if (!model && select.value !== 'deepseek-harness') throw problem('flow.noModel');
    if (picker && select.value === 'opencode' && picker.free.checked && !picker.models.some(entry => entry.id === model && entry.free)) throw problem('models.needFree');
    return model;
  }
  dialog('access').addEventListener('close', async () => {
    const prefixes = [...modelPickers.keys()].filter(prefix => dialog(prefix)?.open && modelPickers.get(prefix).project === accessProject);
    if (!prefixes.length) return;
    try {
      const data = await api(`/api/projects/${accessProject}/access`);
      for (const prefix of prefixes) {
        const picker = modelPickers.get(prefix); picker.policy = data.policy;
        if (prefix !== 'plan') fillAccessProfile(prefix, data.policy, $(`${prefix}-access-profile`).value);
        updateModelGate(prefix);
      }
    } catch (error) { for (const prefix of prefixes) showError(form(prefix), error); }
  });
  async function openPlan() {
    flowProject = state.project;
    open('plan');
    const element = form('plan');
    const data = await loadCatalog(element);
    if (!data) return;
    fillSelect(element.elements.strategy, data.options.strategies, 'cost-first');
    fillSelect(element.elements.risk, data.options.risks, 'medium');
    fillSelect(element.elements.task_type, data.options.task_types, 'general');
    fillSelect(element.elements.topology, data.options.topologies, 'auto');
    try { const access = await api(`/api/projects/${flowProject}/access`); await setupHarnessModels('plan', data.harnesses, access.policy); }
    catch (error) { showError(element, error); }
  }
  $('plan-add-executor').addEventListener('click', () => {
    const cli = $('plan-harness').value; let model;
    try { model = selectedModel('plan'); } catch (error) { showError(form('plan'), error); return; }
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
    const plan = await api(`/api/projects/${flowProject}/plans`, body);
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
    const plan = await api(projectPath(`/plans/${planId}`));
    const item = plan.items.find(entry => entry.id === itemId);
    if (item?.editable && !item.contract_ready) { await openContract(plan, item, true); return; }
    open('launch');
    const element = form('launch'); element.querySelector('[type=submit]').disabled = true;
    try {
      const template = !item?.contract_ready && item?.job_ids?.length ? await api(projectPath(`/jobs/${item.job_ids.at(-1)}/launch-template`)) : null;
      await prepareLaunch(element, planId, itemId, template); element.querySelector('[type=submit]').disabled = false;
    } catch (error) { showError(element, error); }
  }
  async function prepareLaunch(element, planId, itemId, template) {
    const plan = await api(projectPath(`/plans/${planId}`));
    fillAccessProfile('launch', plan.access_policy, template?.access_profile || '');
    const item = plan.items.find(entry => entry.id === itemId);
    if (!item) throw problem('launch.noItem');
    launching = { planId, itemId, item, project: state.project };
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
    const wanted = template ? item.allowed.findIndex(entry => untried.has(entry.display)) : item.allowed.findIndex(entry => entry.display === item.assigned_executor);
    const same = template ? item.allowed.findIndex(entry => entry.cli === template.cli && (entry.model || null) === (template.model || null)) : -1;
    select.value = String(Math.max(0, wanted >= 0 ? wanted : same));
    const sync = () => {
      const entry = item.allowed[Number(select.value)];
      $('launch-confirm-text').textContent = entry ? t('launch.confirm', { executor: entry.display }) : '';
      $('launch-executor-help').textContent = item.allowed.length ? '' : t('launch.noExecutors');
    };
    select.onchange = sync; sync();
    element.elements.task.value = template?.task || item.task || buildTemplate(item.title);
    element.elements.role.value = (template?.role || (item.task ? item.title : '')).slice(0, 80);
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
    if (element.elements.access_profile.value) body.access_profile = element.elements.access_profile.value;
    if (element.elements.role.value.trim()) body.role = element.elements.role.value.trim().slice(0, 80);
    if (element.elements.max_cost_usd.value) body.max_cost_usd = Number(element.elements.max_cost_usd.value);
    if (element.elements.allow_dirty.checked) body.allow_dirty = true;
    if (element.elements.allow_concurrent_workspace.checked) body.allow_concurrent_workspace = true;
    const job = await api(`/api/projects/${launching.project}/plans/${launching.planId}/items/${launching.itemId}/launch`, body);
    state.pendingJob = job.job_id;
  });

  // A browser-created flow starts a bounded planning job, then imports its reviewed proposal.
  $('flow-supervise').addEventListener('change', event => { $('flow-auto-repair').disabled = !event.target.checked; });
  $('new-flow').addEventListener('click', async () => {
    flowProject = state.project;
    open('flow'); $('flow-auto-repair').disabled = false;
    const element = form('flow');
    const data = await loadCatalog(element);
    if (!data) return;
    const access = await api(`/api/projects/${flowProject}/access`);
    fillAccessProfile('flow', access.policy);
    await setupHarnessModels('flow', data.harnesses, access.policy);
    fillSelect(element.elements.strategy, data.options.strategies, 'cost-first');
    fillSelect(element.elements.risk, data.options.risks, 'medium');
  });
  wire('flow', async element => {
    const value = name => element.elements[name].value.trim();
    for (const name of ['title', 'goal']) if (!value(name)) throw problem('err.required', { field: fieldLabel(name) });
    if (!value('cli')) throw problem('flow.noHarness');
    const model = selectedModel('flow');
    if (!element.elements.confirm.checked) throw problem('flow.needConfirm');
    const result = await api(`/api/projects/${flowProject}/flows`, { title: value('title'), goal: value('goal'),
      coordinator: value('cli') + (model ? '=' + model : ''), ...(value('access_profile') ? { access_profile: value('access_profile') } : {}), tasks: lines(value('tasks')),
      executors: lines(value('executors')), expensive_executors: lines(value('expensive_executors')), strategy: value('strategy'), risk: value('risk'), confirm: true, supervise: element.elements.supervise.checked, auto_repair: element.elements.supervise.checked && element.elements.auto_repair.checked, allow_dirty: element.elements.allow_dirty.checked });
    state.pendingJob = result.job_id;
  });
  async function openCoordinator(plan) {
    coordinating = { project: state.project, plan: plan.plan_id, revision: plan.edit_revision };
    open('coordinator'); const element = form('coordinator'); element.querySelector('[type=submit]').disabled = true;
    try {
      const data = await loadCatalog(element); if (!data) return;
      const access = await api(`/api/projects/${coordinating.project}/access`); fillAccessProfile('coordinator', access.policy);
      await setupHarnessModels('coordinator', data.harnesses, access.policy, plan.coordinator?.executor || '');
      if (plan.coordinator?.access_profile) $('coordinator-access-profile').value = plan.coordinator.access_profile;
    } catch (error) { showError(element, error); }
  }
  wire('coordinator', async element => {
    const cli = element.elements.cli.value; const model = selectedModel('coordinator');
    if (!element.elements.confirm.checked) throw problem('flow.needConfirm');
    const plan = await api(`/api/projects/${coordinating.project}/plans/${coordinating.plan}/coordinator`, { revision: coordinating.revision, coordinator: cli + (model ? '=' + model : ''), access_profile: element.elements.access_profile.value || null, confirm: true });
    if (contracting?.plan.plan_id === plan.plan_id && dialog('contract').open) { contracting.plan = plan; contracting.loaded = null; $('contract-proposal').hidden = true; form('contract').querySelector('[type=submit]').disabled = true; renderContractCoordinator(); await pollContract(); }
    state.pendingPlan = plan.plan_id;
  });
  function renderContractCoordinator() {
    if (!contracting) return;
    const plan = contracting.plan;
    $('contract-coordinator').textContent = t('coordinator.identity', { executor: plan.coordinator?.executor || t('coordinator.none') });
    $('contract-pick-coordinator').textContent = t(plan.coordinator?.executor ? 'coordinator.change' : 'coordinator.choose');
    $('contract-pick-coordinator').disabled = !plan.can_edit;
    $('contract-start').disabled = !plan.coordinator?.executor || !plan.can_edit;
    if (!plan.coordinator?.executor) $('contract-status').textContent = t('contract.chooseFirst');
    else if (!plan.can_edit) $('contract-status').textContent = t('planEdit.active');
    else if ([t('contract.chooseFirst'), t('planEdit.active')].includes($('contract-status').textContent)) $('contract-status').textContent = '';
  }
  async function openContract(plan, item, launchAfter = false) {
    open('contract');
    contracting = { project: state.project, plan, item, launchAfter, loaded: null };
    $('contract-objective').value = [item.title, plan.goal.objective && plan.goal.objective !== item.title ? plan.goal.objective : ''].filter(Boolean).join('\n\n').slice(0, 5000);
    $('contract-status').textContent = ''; $('contract-proposal').hidden = true; form('contract').querySelector('[type=submit]').disabled = true;
    renderContractCoordinator(); await pollContract();
  }
  async function pollContract() {
    clearTimeout(contractTimer);
    if (!contracting || !dialog('contract').open) return;
    const context = contracting;
    try {
      const base = `/api/projects/${context.project}/plans/${context.plan.plan_id}/items/${context.item.id}/contract`;
      const draft = await api(base);
      if (context !== contracting || !dialog('contract').open) return;
      if (draft.state === 'running') {
        $('contract-status').textContent = t('contract.waiting'); $('contract-start').disabled = true; $('contract-pick-coordinator').disabled = true;
        contractTimer = setTimeout(pollContract, 1500); return;
      }
      if (draft.state === 'ready') {
        $('contract-status').textContent = t('contract.ready'); $('contract-proposal').hidden = false;
        if (context.loaded !== draft.job_id) {
          $('contract-task').value = draft.task;
          const values = context.plan.executor_policy.allowed.map(entry => entry.display);
          $('contract-executor').replaceChildren(...values.map(value => { const option = node('option', value); option.value = value; return option; }));
          $('contract-executor').value = draft.assigned_executor; $('contract-executor').disabled = Boolean(context.item.assigned_executor);
          form('contract').elements.confirm.checked = false; context.loaded = draft.job_id;
        }
        form('contract').querySelector('[type=submit]').disabled = false;
      } else if (draft.error) { $('contract-status').textContent = draft.error; }
      const plan = await api(`/api/projects/${context.project}/plans/${context.plan.plan_id}`);
      if (context === contracting) { context.plan = plan; renderContractCoordinator(); }
    } catch (error) { showError(form('contract'), error); }
  }
  $('contract-pick-coordinator').addEventListener('click', () => contracting && openCoordinator(contracting.plan));
  $('contract-start').addEventListener('click', async () => {
    if (!contracting) return;
    $('contract-start').disabled = true; showError(form('contract'), null);
    try {
      const objective = $('contract-objective').value.trim(); if (!objective) throw problem('err.required', { field: t('contract.objective') });
      const context = contracting;
      await api(`/api/projects/${context.project}/plans/${context.plan.plan_id}/items/${context.item.id}/contract`, { confirm: true, objective });
      context.loaded = null; $('contract-proposal').hidden = true; form('contract').querySelector('[type=submit]').disabled = true; await pollContract();
    } catch (error) { showError(form('contract'), error); renderContractCoordinator(); }
  });
  dialog('contract').addEventListener('close', () => { clearTimeout(contractTimer); });
  wire('contract', async element => {
    if (!contracting || !element.elements.confirm.checked) throw problem('flow.needConfirm');
    const context = contracting;
    await api(`/api/projects/${context.project}/plans/${context.plan.plan_id}/items/${context.item.id}/contract/accept`, { confirm: true, job_id: context.loaded, task: element.elements.task.value, assigned_executor: element.elements.assigned_executor.value });
    state.pendingPlan = context.plan.plan_id;
    if (context.launchAfter) { dialog('contract').close(); await openLaunch(context.plan.plan_id, context.item.id); }
  });
  async function openPlanEdit(planId) {
    editing = null; open('plan-edit');
    const element = form('plan-edit'); element.querySelector('[type=submit]').disabled = true; $('plan-edit-add').disabled = true;
    $('plan-edit-tasks').replaceChildren();
    try {
      const project = state.project;
      const plan = await api(`/api/projects/${project}/plans/${planId}`);
      if (!plan.can_edit) throw problem('planEdit.active');
      editing = { project, plan: planId, revision: plan.edit_revision, allowed: plan.executor_policy.allowed.map(entry => entry.display), originalAllowed: plan.executor_policy.allowed.map(entry => entry.display), items: plan.items.map(item => ({ ...item })) };
      element.elements.title.value = plan.title || '';
      element.elements.goal.value = plan.goal.objective || '';
      try {
        const data = await loadCatalog(element);
        if (data) {
          const access = await api(`/api/projects/${project}/access`);
          await setupHarnessModels('plan-edit', data.harnesses, access.policy);
        }
      } catch (_) {}
      renderEditExecutors();
      renderEditTasks(); element.querySelector('[type=submit]').disabled = false; $('plan-edit-add').disabled = false;
    } catch (error) { showError(element, error); }
  }
  function renderEditExecutors() {
    if (!editing) return;
    const list = $('plan-edit-executors-list');
    list.replaceChildren(...editing.allowed.map((spec, idx) => {
      const li = node('li', spec + ' ');
      const del = node('button', '×', 'btn-subtle'); del.type = 'button';
      del.style.marginLeft = '8px';
      del.title = t('dialog.cancel');
      del.addEventListener('click', () => {
        editing.allowed.splice(idx, 1);
        renderEditExecutors();
        renderEditTasks();
      });
      li.append(del);
      return li;
    }));
  }
  $('plan-edit-add-executor').addEventListener('click', () => {
    if (!editing) return;
    const cli = $('plan-edit-harness').value;
    let model;
    try { model = selectedModel('plan-edit'); } catch (error) { showError(form('plan-edit'), error); return; }
    if (!cli) return;
    const spec = model ? `${cli}=${model}` : cli;
    if (!editing.allowed.includes(spec)) {
      editing.allowed.push(spec);
      renderEditExecutors();
      renderEditTasks();
    }
    $('plan-edit-model').value = '';
  });
  function renderEditTasks() {
    if (!editing) return;
    $('plan-edit-tasks').replaceChildren(...editing.items.map((item, index) => {
      const section = node('details', null, 'plan-edit-task'); section.open = item.id == null;
      const summary = node('summary', `${index + 1}. ${item.title || t('planEdit.add')}`); section.append(summary);
      if (!item.editable) section.append(node('p', t('planEdit.locked'), 'muted'));
      const titleId = `edit-task-title-${index}`; const titleLabel = node('label', t('planEdit.taskTitle')); titleLabel.htmlFor = titleId;
      const title = node('input'); title.id = titleId; title.maxLength = 300; title.value = item.title || ''; title.readOnly = !item.editable;
      title.addEventListener('input', () => { item.title = title.value; summary.textContent = `${index + 1}. ${item.title}`; });
      const taskId = `edit-task-contract-${index}`; const taskLabel = node('label', t('planEdit.contract')); taskLabel.htmlFor = taskId;
      const task = node('textarea'); task.id = taskId; task.rows = 6; task.maxLength = 200000; task.value = item.task || ''; task.readOnly = !item.editable;
      task.addEventListener('input', () => { item.task = task.value; });
      const executorId = `edit-task-executor-${index}`; const executorLabel = node('label', t('planEdit.executor')); executorLabel.htmlFor = executorId;
      const executor = node('select'); executor.id = executorId; executor.disabled = !item.editable;
      const choices = ['', ...new Set([...editing.allowed, ...(item.assigned_executor ? [item.assigned_executor] : [])])];
      executor.replaceChildren(...choices.map(value => { const option = node('option', value || t('planEdit.unassigned')); option.value = value; return option; }));
      executor.value = item.assigned_executor || '';
      executor.addEventListener('change', () => { item.assigned_executor = executor.value || null; });
      section.append(titleLabel, title, taskLabel, task, node('small', t('planEdit.contractHelp'), 'muted'), executorLabel, executor);
      if (item.editable) {
        const remove = node('button', t('planEdit.remove')); remove.type = 'button'; remove.addEventListener('click', () => { editing.items.splice(index, 1); renderEditTasks(); }); section.append(remove);
      }
      return section;
    }));
  }
  $('plan-edit-add').addEventListener('click', () => {
    if (!editing || editing.items.length >= 50) return;
    editing.items.push({ id: null, title: '', task: '', assigned_executor: null, editable: true }); renderEditTasks();
  });
  wire('plan-edit', async element => {
    if (!editing) return;
    const title = element.elements.title.value.trim(); const goal = element.elements.goal.value.trim();
    if (!title || !goal || !editing.items.length || editing.items.some(item => !item.title.trim())) throw problem('planEdit.required');
    const poolChanged = JSON.stringify([...editing.allowed].sort()) !== JSON.stringify([...editing.originalAllowed].sort());
    if (poolChanged && !confirm(t('planEdit.poolConfirm', { executors: editing.allowed.join(', ') }))) return;
    await api(`/api/projects/${editing.project}/plans/${editing.plan}/edit`, { revision: editing.revision, title, goal,
      executors: editing.allowed, ...(poolChanged ? { confirm: true } : {}),
      items: editing.items.map(item => ({ id: item.id, title: item.title, task: item.task || '', assigned_executor: item.assigned_executor || null, depends_on: item.depends_on || [] })) });
    state.pendingPlan = editing.plan;
  });
  async function openSupervision(plan) {
    supervising = { project: state.project, plan: plan.plan_id };
    open('supervision');
    const element = form('supervision');
    element.querySelector('[type=submit]').disabled = true;
    try {
      const data = await loadCatalog(element);
      if (!data) return;
      const access = await api(`/api/projects/${supervising.project}/access`);
      fillAccessProfile('supervision', access.policy);
      const coordinator = plan.supervision?.coordinator || plan.coordinator || {};
      await setupHarnessModels('supervision', data.harnesses, access.policy, coordinator.executor || '');
      if (coordinator.access_profile) $('supervision-access-profile').value = coordinator.access_profile;
    } catch (error) { showError(element, error); }
  }
  wire('supervision', async element => {
    const cli = element.elements.cli.value;
    const model = selectedModel('supervision');
    if (!element.elements.confirm.checked) throw problem('flow.needConfirm');
    await api(`/api/projects/${supervising.project}/plans/${supervising.plan}/supervision`, { enabled: true, confirm: true,
      reviewer: cli + (model ? '=' + model : ''), access_profile: element.elements.access_profile.value || null, auto_repair: element.elements.auto_repair.checked });
  });
  async function openProposal() {
    proposing = { project: state.project, job: state.agent };
    open('proposal');
    const element = form('proposal');
    $('proposal-tasks').replaceChildren();
    element.querySelector('[type=submit]').disabled = true;
    try {
      const proposal = await api(`/api/projects/${proposing.project}/jobs/${proposing.job}/flow`);
      if (proposal.plan_id) { dialog('proposal').close(); ctx.selectPlan(proposal.plan_id); return; }
      if (proposal.error) throw new FormError(proposal.error);
      if (!proposal.tasks.length) throw problem('flow.waiting');
      proposal.tasks.forEach((task, index) => {
        const section = node('section', null, 'proposal-task');
        section.append(node('small', String(index + 1).padStart(2, '0'), 'eyebrow'), node('h3', task.title), node('p', task.objective));
        const dl = node('dl');
        for (const key of ['scope', 'acceptance', 'validation']) dl.append(node('dt', t(`flow.${key}`)), node('dd', task[key]));
        const label = node('label', t('flow.assignment')); label.htmlFor = `assignment-${index}`;
        const select = node('select'); select.id = label.htmlFor; select.name = 'assignment';
        select.replaceChildren(...proposal.pool.map(value => { const option = node('option', value); option.value = value; option.selected = value === task.executor; return option; }));
        section.append(dl, label, select); if (task.contract) { const details = node('details'); details.append(node('summary', t('planEdit.contract')), node('pre', task.contract, 'engine-detail')); section.append(details); } $('proposal-tasks').append(section);
      });
      element.querySelector('[type=submit]').disabled = false;
    } catch (error) { showError(element, error); }
  }
  wire('proposal', async element => {
    const result = await api(`/api/projects/${proposing.project}/jobs/${proposing.job}/flow/accept`, {
      confirm: true, assignments: [...element.querySelectorAll('[name=assignment]')].map(select => select.value) });
    state.pendingPlan = result.plan_id;
  });

  for (const closer of document.querySelectorAll('dialog [data-close]')) closer.addEventListener('click', () => closer.closest('dialog').close());
  $('new-plan').addEventListener('click', openPlan);

  return { renderJobActions, renderPlanDetail, renderPlansList, renderProjectActions };
};
