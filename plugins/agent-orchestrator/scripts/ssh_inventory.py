"""Read SSH configuration and key-file metadata only. Never run ssh/Match/ProxyCommand or read keys.

This is a deliberately conservative import of direct destinations, not an OpenSSH evaluator.
Conditional/dynamic routing is reported for review rather than guessed.
"""
from __future__ import annotations

import fnmatch
import getpass
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shlex

ALIAS = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
DEFAULT_KEYS = ('id_rsa', 'id_ecdsa', 'id_ecdsa_sk', 'id_ed25519', 'id_ed25519_sk', 'id_mldsa44_ed25519')
DIRECT_KEYS = {'hostname', 'user', 'port', 'identityfile', 'proxycommand', 'proxyjump', 'canonicalizehostname', 'certificatefile', 'identityagent', 'hostkeyalias'}


def matches(alias, patterns):
    return not any(fnmatch.fnmatchcase(alias, item[1:]) for item in patterns if item.startswith('!')) and any(
        fnmatch.fnmatchcase(alias, item) for item in patterns if not item.startswith('!'))



ENVIRONMENTS = ('unknown', 'development', 'testing', 'production')


def organization_path():
    home = os.environ.get('AGENT_ORCHESTRATOR_HOME') or os.environ.get('CLI_AGENT_ORCHESTRATOR_HOME')
    state = Path(home).expanduser().resolve() if home else Path.home() / '.codex' / 'agent-orchestrator'
    return state / 'ssh-inventory-groups.json'


def read_organization():
    path = organization_path()
    if not path.exists():
        return {'version': 1, 'hosts': {}}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('hosts'), dict):
        raise ValueError('Invalid inventory organization')
    return value


def natural_key(value):
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r'(\d+)', value))


def organize(hosts, keys, organization):
    labels = organization.get('hosts', {})
    by_alias = {}
    groups = {}
    for host in hosts:
        label = labels.get(host['name'], {})
        if not isinstance(label, dict):
            label = {}
        group = label.get('group')
        host['group'] = group if isinstance(group, str) and group.strip() else 'Sin clasificar'
        host['environment'] = label.get('environment') if label.get('environment') in ENVIRONMENTS else 'unknown'
        host['root_account'] = host['user'] == 'root'
        by_alias[host['name']] = host
        record = groups.setdefault(host['group'], {'name': host['group'], 'hosts': 0, 'root_accounts': 0})
        record['hosts'] += 1
        record['root_accounts'] += int(host['root_account'])
    for key in keys:
        key['groups'] = sorted({by_alias[name]['group'] for name in key['hosts'] if name in by_alias}, key=natural_key)
        key['shared'] = len(key['hosts']) > 1
        key['group'] = key['groups'][0] if len(key['groups']) == 1 else 'Compartidas entre grupos' if key['groups'] else 'Sin host asociado'
    hosts.sort(key=lambda host: (host['group'] == 'Sin clasificar', natural_key(host['group']), natural_key(host['name'])))
    keys.sort(key=lambda key: (key['group'] == 'Sin host asociado', natural_key(key['group']), natural_key(Path(key['path']).name)))
    return sorted(groups.values(), key=lambda group: (group['name'] == 'Sin clasificar', natural_key(group['name'])))

def inventory(home: Path | None = None, organization: dict | None = None) -> dict:
    home = home or Path.home()
    ssh_dir = home / '.ssh'
    config = ssh_dir / 'config'
    records = []
    aliases = {}
    warnings = []
    visited = set()
    blocked = []
    has_match = False
    byte_count = 0

    def warn(message):
        if message not in warnings:
            warnings.append(message)

    def read_config(path, stack=()):
        nonlocal has_match, byte_count
        resolved = path.resolve()
        if resolved in stack or len(visited) >= 64:
            warn('Include circular o límite de archivos alcanzado; revisa la configuración.')
            blocked.append(['*'])
            return
        if not path.is_file():
            return
        # Includes must explicitly look like configuration files. Broad globs must never open keys.
        if path != config and (path.suffix != '.conf' and path.name != 'config'):
            warn('Se omitió un Include cuyo nombre no es config o *.conf, para no leer archivos de claves.')
            blocked.append(['*'])
            return
        visited.add(resolved)
        if path.stat().st_size + byte_count > 1_000_000:
            warn('La configuración supera el límite de lectura; revisa los destinos manualmente.')
            blocked.append(['*'])
            return
        data = path.read_text(encoding='utf-8', errors='replace')
        byte_count += len(data.encode())
        patterns = ['*']
        conditional = False
        for number, line in enumerate(data.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            match = re.match(r'^\s*([A-Za-z][A-Za-z0-9]*)\s*(?:=\s*)?(.*)$', line)
            if not match:
                warn('Hay líneas no interpretables en la configuración SSH.')
                blocked.append(patterns[:]); continue
            key, raw = match.groups()
            key = key.lower()
            try:
                values = shlex.split(raw, comments=True)
            except ValueError:
                blocked.append(patterns[:]); warn('Hay comillas no cerradas en la configuración SSH.'); continue
            if key == 'host':
                patterns = values
                conditional = False
                for value in values:
                    if ALIAS.fullmatch(value):
                        aliases.setdefault(value, str(path))
            elif key == 'match':
                has_match = True; conditional = True
                warn('Hay bloques Match: no se ejecutan ni se resuelven automáticamente. Sus destinos requieren revisión.')
            elif key == 'include':
                if patterns != ['*'] or conditional:
                    blocked.append(patterns[:]); warn('Un Include condicional requiere revisión manual.'); continue
                for value in values:
                    expanded = os.path.expandvars(value).replace('~/', str(home) + '/', 1) if value.startswith('~/') else os.path.expandvars(value)
                    if '%' in expanded or '$' in expanded:
                        blocked.append(['*']); warn('Un Include contiene variables/tokens no resueltos.'); continue
                    include = Path(expanded)
                    if not include.is_absolute():
                        include = ssh_dir / include
                    for included in sorted(glob.glob(str(include))):
                        read_config(Path(included), (*stack, resolved))
            elif key in DIRECT_KEYS and not conditional and values:
                if key != 'proxycommand' and len(values) != 1:
                    blocked.append(patterns[:]); warn('Una directiva SSH tiene valores ambiguos; requiere revisión.')
                records.append((patterns[:], key, values, str(path), number))

    try:
        read_config(config)
    except (OSError, RuntimeError) as exc:
        warn('No se pudo leer parte de la configuración SSH; no se autoriza automáticamente.')
        blocked.append(['*'])

    keys = {}

    def key_info(path, alias=None, candidate=False):
        literal = str(path)
        identifier = hashlib.sha256(literal.encode()).hexdigest()[:24]
        try:
            exists = path.is_file()
        except OSError:
            exists = False
        item = keys.setdefault(literal, {'id': identifier, 'path': literal, 'exists': exists, 'hosts': [], 'candidate': candidate, 'referenced': not candidate})
        if alias and alias not in item['hosts']:
            item['hosts'].append(alias); item['candidate'] = False; item['referenced'] = True
        return item

    # Keep static IdentityFile references even in wildcard-only configurations.
    for _patterns, keyword, values, _source, _line in records:
        if keyword != 'identityfile':
            continue
        for value in values:
            expanded = os.path.expandvars(value).replace('%d', str(home)).replace('%u', getpass.getuser())
            if expanded.startswith('~/'):
                expanded = str(home) + expanded[1:]
            if value.lower() != 'none' and '%' not in expanded and '$' not in expanded and Path(expanded).is_absolute():
                try:
                    key_info(Path(expanded).resolve())
                except (OSError, RuntimeError):
                    warn('No se pudo resolver una identidad referenciada por la configuración.')
    hosts = []
    for alias, source in aliases.items():
        options = {}
        identities = []
        for patterns, key, values, _path, _line in records:
            if matches(alias, patterns):
                if key == 'identityfile':
                    identities.extend(values)
                elif key not in options:
                    options[key] = values[0]
        host = options.get('hostname', alias).replace('%h', alias).replace('%%', '%')
        user = options.get('user', getpass.getuser())
        try:
            port = int(options.get('port', '22'))
        except ValueError:
            port = 0
        reasons = []
        if has_match or any(matches(alias, patterns) for patterns in blocked):
            reasons.append('Configuración dinámica o incompleta; revisar manualmente.')
        for name in ('proxycommand', 'proxyjump', 'certificatefile', 'hostkeyalias', 'identityagent'):
            if options.get(name, 'none').lower() != 'none':
                reasons.append(f'{name}: necesita revisión; el conector sólo admite conexiones directas.')
        if options.get('canonicalizehostname', 'no').lower() not in {'no', 'false'}:
            reasons.append('Canonicalización de host no resuelta.')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', host) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', user) or not 1 <= port <= 65535:
            reasons.append('Host, usuario o puerto no resuelto.')
        paths = []
        if not identities:
            identities = [str(ssh_dir / name) for name in DEFAULT_KEYS if (ssh_dir / name).is_file()]
        for identity in identities:
            if identity.lower() == 'none':
                continue
            expanded = os.path.expandvars(identity)
            tokens = {'d': str(home), 'h': host, 'n': alias, 'r': user, 'p': str(port), 'u': getpass.getuser(), '%': '%'}
            unresolved = []
            def expand_token(match):
                if match[1] not in tokens:
                    unresolved.append(match[1]); return match[0]
                return tokens[match[1]]
            expanded = re.sub(r'%(.)', expand_token, expanded)
            if expanded.startswith('~/'):
                expanded = str(home) + expanded[1:]
            if unresolved or '$' in expanded or not Path(expanded).is_absolute():
                reasons.append('Ruta de identidad no resuelta.'); continue
            try:
                path = Path(expanded).resolve()
            except (OSError, RuntimeError):
                reasons.append('No se pudo resolver la ruta de identidad.'); continue
            if '%' in str(path):
                reasons.append('La ruta de identidad requiere revisión de tokens.')
            item = key_info(path, alias)
            if str(path) not in paths:
                paths.append(str(path))
        if not any(keys[path]['exists'] for path in paths):
            reasons.append('No hay una identidad local disponible.')
        hosts.append({'name': alias, 'host': host, 'user': user, 'port': port,
                      'identity_files': [{'path': path, 'exists': keys[path]['exists']} for path in paths],
                      'source': source, 'eligible': not reasons, 'reasons': sorted(set(reasons))})

    # Metadata-only candidates, not a claim to have validated their cryptographic format.
    try:
        for path in sorted(ssh_dir.iterdir()) if ssh_dir.is_dir() else []:
            name = path.name
            if not path.is_file() or name.endswith(('.pub', '.conf')) or name in {'config', 'known_hosts', 'known_hosts.old', 'authorized_keys', 'authorized_keys2'}:
                continue
            if name in DEFAULT_KEYS or path.suffix in {'.pem', '.key'} or (ssh_dir / (name + '.pub')).is_file():
                key_info(path.resolve(), candidate=True)
    except OSError:
        warn('No se pudo completar el inventario de metadatos de ~/.ssh.')
    if organization is None:
        try:
            organization = read_organization()
        except (ValueError, OSError):
            organization = {'hosts': {}}
            warn('No se pudo cargar la organización del inventario. Los permisos no cambian.')
    key_rows = list(keys.values())
    groups = organize(hosts, key_rows, organization)
    return {'hosts': hosts, 'keys': key_rows, 'groups': groups, 'warnings': warnings, 'config_found': config.is_file()}
