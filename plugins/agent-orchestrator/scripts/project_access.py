"""Pinned project access policies. Restricts CLI tools, not the operating system or trusted MCPs."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from project_identity import project_id, project_root

SUPPORTED = {'claude-code', 'claude'}
SSH_ALIAS = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}')
HOST = re.compile(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}')
USER = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}')
TOOLS = ['Read', 'Edit', 'Write', 'Glob', 'Grep']


class AccessError(Exception):
    pass


def read_object(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise AccessError('Expected a JSON object')
    return value


def policy_for(workspace: Path, state_home: Path) -> dict:
    record = read_object(state_home / 'projects.json').get(project_id(str(workspace)), {})
    if not isinstance(record, dict) or not isinstance(record.get('access_policy', {}), dict):
        raise AccessError('Invalid saved project access policy')
    return record.get('access_policy', {'enabled': False})


def mcp_sources(workspace: Path) -> list[tuple[str, dict]]:
    config = read_object(Path.home() / '.claude.json')
    projects = config.get('projects') or {}
    local = projects.get(str(project_root(str(workspace))), {}) if isinstance(projects, dict) else {}
    return [
        ('Usuario de Claude', config.get('mcpServers', {})),
        ('Proyecto (.mcp.json)', read_object(project_root(str(workspace)) / '.mcp.json').get('mcpServers', {})),
        ('Local de Claude para este proyecto', local.get('mcpServers', {}) if isinstance(local, dict) else {}),
    ]


def mcp_definitions(workspace: Path) -> dict:
    """Claude precedence: local > project > user. Credentials stay out of the public catalog."""
    servers = {}
    for _source, root in mcp_sources(workspace):
        if not isinstance(root, dict):
            continue
        for name, definition in root.items():
            if NAME.fullmatch(name) and name != 'ao_control' and isinstance(definition, dict):
                if definition.get('disabled'):
                    servers.pop(name, None)
                else:
                    servers[name] = definition
    return servers


def mcp_origins(workspace: Path) -> dict:
    origins = {}
    for source, root in mcp_sources(workspace):
        if isinstance(root, dict):
            for name in root:
                origins[name] = source
    return origins


def validate_hosts(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 100:
        raise AccessError('SSH: usa una lista de hasta 100 destinos.')
    hosts = []
    seen = set()
    for raw in value:
        required = {'name', 'host', 'user', 'identity_file'}
        if not isinstance(raw, dict) or not required.issubset(raw) or set(raw) - required - {'port', 'allow_root'}:
            raise AccessError('SSH: cada destino necesita name, host, user e identity_file; port es opcional.')
        if not isinstance(raw['name'], str) or not SSH_ALIAS.fullmatch(raw['name']) or raw['name'] in seen:
            raise AccessError('SSH: alias inválido o repetido.')
        if not isinstance(raw['host'], str) or not HOST.fullmatch(raw['host']):
            raise AccessError('SSH: usa un hostname o IP literal, sin opciones ni comodines.')
        if not isinstance(raw['user'], str) or not USER.fullmatch(raw['user']):
            raise AccessError('SSH: usuario inválido.')
        port = raw.get('port', 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise AccessError('SSH: puerto inválido.')
        identity = raw['identity_file']
        if not isinstance(identity, str) or len(identity) > 2000 or any(char in identity for char in '\x00\n\r%'):
            raise AccessError('SSH: ruta de clave inválida.')
        key = Path(identity).expanduser()
        if not key.is_absolute() or not key.is_file():
            raise AccessError('SSH: la clave indicada debe existir en este equipo. No pegues su contenido.')
        if 'allow_root' in raw and type(raw['allow_root']) is not bool:
            raise AccessError('SSH: allow_root debe ser verdadero o falso.')
        seen.add(raw['name'])
        hosts.append({**raw, 'port': port, 'identity_file': str(key.resolve())})
    return hosts


def normalize_policy(body: dict, workspace: Path, existing: dict | None = None) -> dict:
    if not isinstance(body, dict) or not {'enabled', 'mcp_servers', 'ssh_hosts'}.issubset(body) or set(body) - {'enabled', 'mcp_servers', 'ssh_hosts', 'agent_profiles', 'per_agent'} or type(body.get('enabled')) is not bool:
        raise AccessError('Configuración de acceso inválida.')
    names = body['mcp_servers']
    if not isinstance(names, list) or len(names) > 30 or any(not isinstance(name, str) or not NAME.fullmatch(name) or name == 'ao_control' for name in names) or len(set(names)) != len(names):
        raise AccessError('Selecciona MCP válidos, sin duplicados.')
    hosts = validate_hosts(body['ssh_hosts'])
    definitions = mcp_definitions(workspace)
    if any(name not in definitions for name in names):
        raise AccessError('Un MCP seleccionado ya no existe en la configuración de Claude de este proyecto.')
    if not body['enabled'] and (names or hosts):
        raise AccessError('Activa el modo restringido para guardar listas de acceso.')
    per_agent = body.get('per_agent', 'agent_profiles' in body or 'agent_profiles' in (existing or {}))
    if type(per_agent) is not bool:
        raise AccessError('Control por agente inválido.')
    profiles = body.get('agent_profiles', (existing or {}).get('agent_profiles', {}) if body['enabled'] else {})
    if not isinstance(profiles, dict) or len(profiles) > 30:
        raise AccessError('Usa hasta 30 perfiles de acceso de agente.')
    host_names = {host['name'] for host in hosts}
    for profile_name, grant in profiles.items():
        if not isinstance(profile_name, str) or not NAME.fullmatch(profile_name) or not isinstance(grant, dict) or set(grant) != {'mcp_servers', 'ssh_hosts'}:
            raise AccessError('Perfil de acceso inválido.')
        for key, ceiling in (('mcp_servers', set(names)), ('ssh_hosts', host_names)):
            values = grant[key]
            if not isinstance(values, list) or any(not isinstance(value, str) or value not in ceiling for value in values) or len(values) != len(set(values)):
                raise AccessError('Un perfil sólo puede usar recursos autorizados para este proyecto, sin duplicados.')
    if profiles and not per_agent:
        raise AccessError('Activa el control por agente para guardar perfiles.')
    if not body['enabled'] and profiles:
        raise AccessError('Activa el modo restringido para usar perfiles.')
    return {'enabled': body['enabled'], 'mcp_servers': {name: definitions[name] for name in names}, 'ssh_hosts': hosts,
            **({'agent_profiles': profiles} if per_agent and body['enabled'] else {})}


def public_policy(policy: dict) -> dict:
    return {'enabled': bool(policy.get('enabled')), 'mcp_servers': list(policy.get('mcp_servers', {})),
            'ssh_hosts': policy.get('ssh_hosts', []), 'agent_profiles': policy.get('agent_profiles', {}), 'per_agent': 'agent_profiles' in policy, 'supported_clis': sorted(SUPPORTED)}



def for_agent(policy: dict, profile: str | None) -> dict:
    profiles = policy.get('agent_profiles', {})
    if profile and (not policy.get('enabled') or profile not in profiles):
        raise AccessError('El perfil de acceso no existe en este proyecto o el modo restringido está desactivado.')
    if 'agent_profiles' not in policy:
        return policy
    # Once profiles are configured, an unspecified profile never inherits the project ceiling.
    grant = profiles[profile] if profile else {'mcp_servers': [], 'ssh_hosts': []}
    return {**policy, 'profile': profile, '_protected_identities': [host['identity_file'] for host in policy.get('ssh_hosts', [])], 'mcp_servers': {name: value for name, value in policy.get('mcp_servers', {}).items() if name in grant['mcp_servers']},
            'ssh_hosts': [host for host in policy.get('ssh_hosts', []) if host['name'] in grant['ssh_hosts']]}

def require_supported(cli: str, profile: dict, builtin: dict) -> None:
    if not sys.platform.startswith('linux'):
        raise AccessError('Este adaptador de acceso restringido está verificado en Linux.')
    if any(Path('/etc/claude-code', name).exists() for name in ('managed-settings.json', 'managed-mcp.json')) or list(Path('/etc/claude-code/managed-settings.d').glob('*.json')):
        raise AccessError('Hay políticas administradas de Claude en este equipo. Su precedencia requiere revisión antes de garantizar esta configuración.')
    if cli not in SUPPORTED or profile != builtin:
        raise AccessError('Este proyecto exige acceso restringido. Solo el adaptador integrado de Claude Code está verificado; no se iniciará este CLI.')
    executable = shutil.which(profile['argv'][0])
    if not executable:
        raise AccessError('Claude Code no está instalado.')
    try:
        result = subprocess.run([executable, '--help'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AccessError('No se pudo verificar la versión de Claude Code. No se iniciará el agente.') from exc
    if result.returncode or any(flag not in result.stdout for flag in ('--restricted', '--strict-mcp-config', '--tools', '--settings', '--setting-sources', '--no-chrome', '--disable-slash-commands')):
        raise AccessError('Actualiza Claude Code: esta versión no expone todos los controles de acceso requeridos.')


def prepare(policy: dict, path: Path, state_home: Path, write_json) -> dict:
    """Materialize private, per-job configs. The restricted CLI cannot access this directory."""
    servers = dict(policy.get('mcp_servers', {}))
    # Preserve only the approved connection settings, never project hooks or other settings.
    write_json(path / 'access-manifest.json', {'state_home': str(state_home), 'job_id': path.name,
        'ssh_hosts': policy.get('ssh_hosts', []), 'identity_agent': os.environ.get('SSH_AUTH_SOCK')})
    servers['ao_control'] = {'command': sys.executable, 'args': [str(Path(__file__).with_name('access_broker.py')), str(path / 'access-manifest.json')]}
    write_json(path / 'access-mcp.json', {'mcpServers': servers})
    allow = TOOLS + [f'mcp__{name}__*' for name in servers]
    protected = ['~/.ssh/**', '~/.claude.json', '~/.claude/**', '~/.codex/**']
    def escape_path(value):
        return ''.join('\\' + char if char in '\\*?[]' else char for char in value)
    protected += ['/' + escape_path(key) for key in policy.get('_protected_identities', [host['identity_file'] for host in policy.get('ssh_hosts', [])])]
    protected.append('/' + escape_path(str(state_home)) + '/**')
    deny = ['Bash', 'PowerShell', 'Agent', 'Task', 'WebFetch', 'WebSearch', 'NotebookEdit']
    deny += [f'{tool}({item})' for item in protected for tool in ('Read', 'Edit')]
    write_json(path / 'access-settings.json', {'permissions': {'allow': allow,
        'deny': deny,
        'disableBypassPermissionsMode': 'disable'}})
    return {'enabled': True, 'profile': policy.get('profile'), 'per_agent': 'agent_profiles' in policy, 'mcp_servers': list(policy.get('mcp_servers', {})),
            'ssh_hosts': [host['name'] for host in policy.get('ssh_hosts', [])], 'terminal': 'disabled'}


def apply_command(command: list[str], path: Path) -> list[str]:
    return [*command, '--restricted', '--strict-mcp-config', '--mcp-config', str(path / 'access-mcp.json'),
            '--settings', str(path / 'access-settings.json'), '--setting-sources', '',
            '--tools', ','.join(TOOLS), '--disable-slash-commands', '--no-chrome']


def restricted_environment(extra: dict | None) -> dict:
    env = {**os.environ, **(extra or {})}
    for key in ('SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'SSH_ASKPASS', 'GIT_SSH', 'GIT_SSH_COMMAND'):
        env.pop(key, None)
    return env


def preamble() -> str:
    return ('# Restricted project access\n\n'
            'Use mcp__ao_control__event for phase/message updates and mcp__ao_control__ask for questions. '
            'There is no local terminal. Do not try to execute Python or shell commands. '
            'SSH is available only via mcp__ao_control__ssh_exec and its approved aliases. '
            'If validation requires a terminal and no approved MCP provides it, report that limitation; do not claim tests passed.\n\n# Assigned task\n\n')
