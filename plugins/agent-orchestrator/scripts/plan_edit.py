"""Versioned plan edits: stable task IDs, immutable attempted work, no executor expansion."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import time

import cli_agent_job as jobs
from project_identity import project_id


def revision(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def active_jobs(plan):
    project = project_id(plan['workspace'])
    active = []
    for meta in jobs.jobs_dir().glob('*/meta.json'):
        try:
            status = jobs.get_status(meta.parent)
            if status['execution_state'] in {'starting', 'running', 'needs_input', 'cancelling'} and (status.get('plan_id') == plan['plan_id'] or project_id(status['workspace']) == project):
                active.append(meta.parent.name)
        except (jobs.RunnerError, KeyError, OSError, ValueError):
            continue
    return active


def editable(item):
    return item.get('state') == 'pending' and not item.get('job_ids')


def write_document(path, content):
    temporary = path.with_name('.plan-edit-' + secrets.token_hex(8))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def edit(plan_id, body):
    import panel_actions as actions
    try:
        actions.only_fields(body, ('revision', 'title', 'goal', 'items'), ('coordinator', 'contract_source'))
        expected = actions.text(body, 'revision', limit=64, single_line=True)
        title = actions.text(body, 'title', limit=200, single_line=True)
        goal = actions.text(body, 'goal', limit=5000)
        coordinator = None
        if 'coordinator' in body:
            settings = actions.only_fields(body['coordinator'], ('executor',), ('access_profile',))
            spec = actions.text(settings, 'executor', limit=200, single_line=True)
            if not actions.EXECUTOR_RE.fullmatch(spec):
                raise actions.invalid('coordinator')
            selected = jobs.parse_executor_spec(spec, expensive_approved=True)
            profiles = jobs.load_profiles(jobs.config_path(None))
            profile = profiles.get(selected['cli'])
            if not profile or not jobs.resolve_profile_executable(profile)[1]:
                raise actions.invalid('coordinator', 'Selecciona un CLI de orquestador instalado.')
            if profile.get('model_args') and not selected.get('model'):
                raise actions.invalid('coordinator', 'Selecciona un modelo explícito para el orquestador.')
            coordinator = {'executor': spec, 'access_profile': actions.text(settings, 'access_profile', required=False, limit=64, single_line=True)}
        rows = body['items']
        if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
            raise actions.invalid('items', 'El plan necesita entre 1 y 50 tareas.')
        clean = []
        for row in rows:
            actions.only_fields(row, ('id', 'title', 'task', 'assigned_executor'))
            clean.append({'id': actions.text(row, 'id', required=False, limit=128, single_line=True),
                          'title': actions.text(row, 'title', limit=300, single_line=True),
                          'task': actions.text(row, 'task', required=False, limit=200000) or '',
                          'assigned_executor': actions.text(row, 'assigned_executor', required=False, limit=200, single_line=True)})
    except actions.ActionError as exc:
        raise jobs.RunnerError(str(exc)) from exc
    registry_path = jobs.state_home() / 'projects.json'
    registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with jobs.record_lock(registry_path):
        path = jobs.plan_dir(plan_id)
        with jobs.record_lock(path):
            _, plan = jobs.read_plan(plan_id)
            if revision(plan) != expected:
                raise jobs.RunnerError('plan_conflict: El plan cambió desde que abriste el editor. Cierra y vuelve a abrir Editar plan para cargar la versión actual.')
            if body.get('contract_source') is not None:
                try:
                    source = actions.only_fields(body['contract_source'], ('item_id', 'job_id'))
                    source_item = actions.text(source, 'item_id', limit=64, single_line=True)
                    jobs.validate_identifier(source_item, 'item')
                    source_job = actions.text(source, 'job_id', limit=64, single_line=True)
                    request = jobs.read_json(path / 'contracts' / (source_item + '.json'))
                    if request.get('job_id') != source_job or request.get('revision') != expected:
                        raise jobs.RunnerError('plan_conflict: Hay un borrador más reciente. Vuelve a abrir el contrato para revisarlo.')
                except actions.ActionError as exc:
                    raise jobs.RunnerError(str(exc)) from exc
            registry = jobs.read_json(registry_path) if registry_path.exists() else {}
            project = registry.get(project_id(plan['workspace']), {})
            if project.get('archived') or project.get('deleted'):
                raise jobs.RunnerError('Restaura el proyecto antes de editar sus planes.')
            if active_jobs(plan):
                raise jobs.RunnerError('plan_active: Hay agentes o revisores en ejecución en este proyecto. Espera a que terminen antes de editar el plan.')
            if coordinator:
                import project_access
                policy = project_access.policy_for(Path(plan['workspace']), jobs.state_home())
                if policy.get('enabled'):
                    selected = jobs.parse_executor_spec(coordinator['executor'], expensive_approved=True)
                    try:
                        project_access.for_agent(policy, coordinator['access_profile'])
                        project_access.require_supported(selected['cli'], profiles[selected['cli']], jobs.validate_profile(selected['cli'], jobs.BUILTIN_PROFILES.get(selected['cli'], {})))
                    except project_access.AccessError as exc:
                        raise jobs.RunnerError(str(exc)) from exc
            original = copy.deepcopy(plan)
            existing = {item['id']: item for item in plan.get('items', [])}
            allowed = {entry['display'] for entry in plan.get('executor_policy', {}).get('allowed', [])}
            ids, items = set(), []
            for row in clean:
                item_id = row['id']
                if item_id:
                    if item_id not in existing or item_id in ids:
                        raise jobs.RunnerError('Identificador de tarea desconocido o repetido.')
                    ids.add(item_id)
                    item = copy.deepcopy(existing[item_id])
                    if not editable(item):
                        if any((item.get(key) or '') != (row.get(key) or '') for key in ('title', 'task', 'assigned_executor')):
                            raise jobs.RunnerError('Las tareas con ejecuciones o ya completadas conservan su contenido original. Añade una tarea nueva para ampliar el trabajo.')
                        items.append(item)
                        continue
                else:
                    item_id = 'item-' + secrets.token_hex(6)
                    item = {'id': item_id, 'state': 'pending', 'job_ids': [], 'evidence': []}
                if row['assigned_executor'] and row['assigned_executor'] not in allowed:
                    raise jobs.RunnerError('El agente asignado debe pertenecer al conjunto de ejecutores ya autorizado.')
                if row['task']:
                    problems = jobs.validate_task_packet(row['task'])
                    if problems:
                        raise jobs.RunnerError('Contrato de tarea incompleto: ' + '; '.join(problems))
                item.update(title=row['title'], task=row['task'], assigned_executor=row['assigned_executor'])
                items.append(item)
            removed = set(existing) - ids
            linked = {record.get('checklist_item') for record in jobs.read_feedback() if record.get('plan_id') == plan_id}
            if any(not editable(existing[key]) or key in linked for key in removed):
                raise jobs.RunnerError('No se pueden quitar tareas con ejecuciones, decisiones vinculadas o estado completado.')
            if goal != plan.get('goal', {}).get('objective') and all(item.get('state') == 'done' for item in items):
                raise jobs.RunnerError('Para cambiar el objetivo de un plan terminado, añade una tarea pendiente que permita verificar el nuevo trabajo.')
            old_document = (path / 'plan.md').read_text(encoding='utf-8')
            neutral = actions.neutralize
            document = old_document
            replacements = {'Goal': neutral(goal), 'Checklist': '\n'.join(('- [x] ' if item['state'] == 'done' else '- [ ] ') + neutral(item['title']) for item in items)}
            for heading, body_text in replacements.items():
                match = re.search(r'(?mi)^##[ \t]+' + re.escape(heading) + r'[ \t]*\r?\n', document)
                if not match:
                    raise jobs.RunnerError('El documento original no contiene la sección ' + heading)
                following = re.search(r'(?m)^#{1,2}[ \t]+', document[match.end():])
                end = match.end() + following.start() if following else len(document)
                document = document[:match.end()] + body_text + '\n\n' + document[end:]
            if re.search(r'(?m)^#[ \t]+', document):
                document = re.sub(r'(?m)^#[ \t]+[^\r\n]*', lambda _: '# ' + neutral(title), document, count=1)
            else:
                document = '# ' + neutral(title) + '\n\n' + document
            problems = jobs.validate_named_sections(document, jobs.REQUIRED_PLAN_SECTIONS)
            if problems:
                raise jobs.RunnerError('El documento del plan necesita corregirse antes de guardar: ' + '; '.join(problems))
            edit_id = 'edit-' + str(time.time_ns()) + '-' + secrets.token_hex(3)
            jobs.write_json(path / 'revisions' / (edit_id + '.json'), {'id': edit_id, 'at': jobs.utc_now(), 'source': 'local-user', 'plan': original, 'document': old_document})
            if coordinator:
                plan['coordinator'] = coordinator
            plan.update(title=title, items=items, last_edit={'id': edit_id, 'at': jobs.utc_now()})
            plan.setdefault('goal', {})['objective'] = goal
            plan['plan_sha256'] = hashlib.sha256(document.encode()).hexdigest()
            try:
                write_document(path / 'plan.md', document)
                jobs.save_plan(path, plan)
            except (OSError, jobs.RunnerError):
                write_document(path / 'plan.md', old_document)
                raise
            return jobs.read_plan(plan_id)[1]
