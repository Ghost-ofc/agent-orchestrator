"""Read-only model discovery. Return model names/IDs, never raw CLI diagnostics or credentials."""
from __future__ import annotations
import json
from pathlib import Path
import re
import subprocess
import threading
import time

import cli_agent_job as jobs

CACHE = {}
LOCK = threading.Lock()
MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,199}')


def row(model_id, name=None, cost=None):
    if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id) or model_id.startswith(('sk-', 'ghp_', 'gho_')):
        return None
    free = model_id.endswith('-free')
    if isinstance(cost, dict) and all(isinstance(cost.get(key), (int, float)) and not isinstance(cost.get(key), bool) for key in ('input', 'output')):
        free = cost['input'] == 0 and cost['output'] == 0
    return {'id': model_id, 'name': name[:200] if isinstance(name, str) and name.strip() else model_id, 'free': free}


def parse_models(output):
    rows = []
    # OpenCode --verbose emits an ID line followed by a complete JSON model definition.
    decoder = json.JSONDecoder()
    for match in re.finditer(r'(?m)^([A-Za-z0-9][A-Za-z0-9._:/+@-]{0,199})\r?\n(?=\{)', output):
        try:
            value, _ = decoder.raw_decode(output, match.end())
            if isinstance(value, dict):
                item = row(match[1], value.get('name'), value.get('cost'))
                if item:
                    rows.append(item)
        except ValueError:
            continue
    if rows:
        return rows
    try:
        value = json.loads(output)
        models = value.get('models') if isinstance(value, dict) else value
        if isinstance(models, list):
            for value in models:
                if isinstance(value, dict) and value.get('visibility') not in ('hidden', 'hide'):
                    item = row(value.get('slug') or value.get('id') or value.get('model'), value.get('display_name') or value.get('name'), value.get('cost'))
                    if item:
                        rows.append(item)
            return rows
    except ValueError:
        pass
    for line in output.splitlines():
        parts = line.strip().split('\t', 1)
        model_id = parts[0]
        if model_id.casefold() in {'id', 'model', 'models', 'name'}:
            continue
        item = row(model_id, parts[1] if len(parts) > 1 else None)
        if item:
            rows.append(item)
    return rows


def catalog(cli, refresh=False):
    profiles = jobs.load_profiles(jobs.config_path(None))
    if not isinstance(cli, str) or not jobs.AGENT_NAME_RE.fullmatch(cli) or cli not in profiles:
        raise jobs.RunnerError('CLI desconocido.')
    profile = profiles[cli]
    key = (str(jobs.state_home()), cli, json.dumps(profile, sort_keys=True))
    # Serialize cache fills, independently of mutating engine actions.
    with LOCK:
        cached = CACHE.get(key)
        if cached and not refresh and time.monotonic() - cached[0] < 120:
            return cached[1]
        _, executable = jobs.resolve_profile_executable(profile)
        configured = [item for model in profile.get('models', []) if (item := row(model))]
        result = {'cli': cli, 'available': bool(executable), 'models': configured, 'source': 'configured', 'warning': None}
        command = profile.get('discover_models_argv')
        if command and executable:
            command = jobs.replace_placeholders(command, {'workspace': str(Path.home())})
            if cli == 'opencode' and command == ['opencode', 'models']:
                command = [*command, 'opencode', '--verbose', '--pure']
            try:
                process = subprocess.run(command, cwd=Path.home(), capture_output=True, text=True, timeout=12, stdin=subprocess.DEVNULL, check=False)
                if process.returncode == 0 and len(process.stdout) <= 4 * 1024 * 1024:
                    found = parse_models(jobs.ANSI_RE.sub('', process.stdout))
                    if found:
                        result.update(models=list({item['id']: item for item in [*configured, *found]}.values()), source='live')
                    else:
                        result['warning'] = 'El CLI no devolvió un catálogo reconocible. Puedes introducir un ID de modelo explícito.'
                else:
                    result['warning'] = 'No se pudo consultar el catálogo del CLI. Se muestran sus modelos configurados.'
            except (OSError, subprocess.TimeoutExpired):
                result['warning'] = 'El catálogo no respondió a tiempo. Se muestran los modelos configurados.'
        if not executable:
            result['source'] = 'unavailable'
        if len(CACHE) >= 64:
            CACHE.clear()
        CACHE[key] = (time.monotonic(), result)
        return result
