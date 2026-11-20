"""Minimal stdio MCP for a job's coordination channel and approved direct SSH destinations.

No shell tool, no generic subprocess arguments, no config lookup, and no destination supplied by
an agent is passed to ssh. Approved MCP services and remote accounts remain their own trust boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import selectors
import time

import cli_agent_job as jobs

MAX_MESSAGE = 65536


def tool(name, description, properties, required):
    return {'name': name, 'description': description, 'inputSchema': {'type': 'object',
            'properties': properties, 'required': required, 'additionalProperties': False}}


def available_tools(manifest):
    tools = [tool('event', 'Report progress for this job.', {'phase': {'type': 'string'}, 'message': {'type': 'string'}}, ['phase', 'message']),
             tool('ask', 'Ask the user and wait for their answer.', {'question': {'type': 'string'}}, ['question'])]
    if manifest.get('ssh_hosts'):
        tools.append(tool('ssh_exec', 'Run a remote command on an approved direct SSH destination. No forwarding or custom ssh options.',
                          {'host': {'type': 'string', 'enum': [host['name'] for host in manifest['ssh_hosts']]},
                           'command': {'type': 'string', 'maxLength': 10000}, 'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 120}}, ['host', 'command']))
    return tools


def ssh_command(manifest, args):
    if set(args) - {'host', 'command', 'timeout_seconds'} or not isinstance(args.get('host'), str):
        raise ValueError('Invalid SSH request')
    host = next((entry for entry in manifest.get('ssh_hosts', []) if entry['name'] == args['host']), None)
    if host is None:
        raise ValueError('SSH destination is not approved for this project')
    command = args.get('command')
    timeout = args.get('timeout_seconds', 60)
    if not isinstance(command, str) or not command.strip() or len(command) > 10000 or '\x00' in command or type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError('Invalid SSH command or timeout')
    binary = shutil.which('ssh', path='/usr/bin:/bin')
    if not binary:
        raise ValueError('OpenSSH is not installed')
    argv = [binary, '-F', '/dev/null', '-T', '-oBatchMode=yes', '-oStrictHostKeyChecking=yes',
            '-oClearAllForwardings=yes', '-oForwardAgent=no', '-oForwardX11=no', '-oPermitLocalCommand=no',
            '-oProxyCommand=none', '-oProxyJump=none', '-oControlMaster=no', '-oControlPath=none',
            '-oIdentitiesOnly=yes', '-oPasswordAuthentication=no', '-oKbdInteractiveAuthentication=no',
            '-oConnectTimeout=15', '-oServerAliveInterval=10', '-oServerAliveCountMax=2',
            '-oIdentityAgent=' + (manifest.get('identity_agent') or 'none'),
            '-i', host['identity_file'], '-p', str(host['port']), '-l', host['user'], '--', host['host'], command]
    return argv, timeout


def run_ssh(manifest, args):
    argv, timeout = ssh_command(manifest, args)
    path = jobs.job_dir(manifest['job_id'])
    jobs.add_event(jobs.channel_dir(path), 'ssh_started', host=args['host'], command_sha256=hashlib.sha256(args['command'].encode()).hexdigest())
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                             env={key: value for key, value in os.environ.items() if key not in {'SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'SSH_ASKPASS', 'GIT_SSH', 'GIT_SSH_COMMAND'}})
    timed_out = False
    truncated = False
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as streams:
            streams.register(child.stdout, selectors.EVENT_READ, 'stdout')
            streams.register(child.stderr, selectors.EVENT_READ, 'stderr')
            while streams.get_map() and not truncated:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in streams.select(min(remaining, .2)):
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        streams.unregister(key.fileobj)
                        continue
                    target = output[key.data]
                    target.extend(data[:max(0, 32769 - len(target))])
                    if len(target) > 32768:
                        truncated = True
                        break
        if timed_out or truncated:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        child.stdout.close(); child.stderr.close()
    jobs.add_event(jobs.channel_dir(path), 'ssh_finished', host=args['host'], exit_code=child.returncode, timed_out=timed_out)
    return {'exit_code': child.returncode, 'timed_out': timed_out,
            'stdout': output['stdout'][:32768].decode(errors='replace'), 'stderr': output['stderr'][:32768].decode(errors='replace'), 'truncated': truncated}


def invoke(manifest, name, args):
    if not isinstance(args, dict):
        raise ValueError('Expected tool arguments')
    if name == 'ssh_exec':
        return run_ssh(manifest, args)
    if name == 'event':
        if set(args) != {'phase', 'message'} or any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in args.values()):
            raise ValueError('Invalid phase/message')
        return jobs.add_event(jobs.channel_dir(jobs.job_dir(manifest['job_id'])), 'worker_progress', **args)
    if name == 'ask':
        if set(args) != {'question'} or not isinstance(args['question'], str) or not args['question'].strip() or len(args['question']) > 5000:
            raise ValueError('Invalid question')
        result = subprocess.run([sys.executable, str(Path(jobs.__file__)), 'ask', manifest['job_id'],
                                 '--question=' + args['question'], '--wait-seconds=1800'], capture_output=True, text=True, timeout=1810, check=False)
        return {'exit_code': result.returncode, 'answer': result.stdout[-20000:], 'error': result.stderr[-1000:]}
    raise ValueError('Unknown tool')


def serve(manifest):
    os.environ['AGENT_ORCHESTRATOR_HOME'] = manifest['state_home']
    while True:
        line = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
        if not line:
            return
        if len(line) > MAX_MESSAGE:
            return
        request = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError('Expected JSON-RPC object')
            if 'id' not in request:
                continue
            method = request.get('method')
            params = request.get('params', {})
            if method == 'initialize':
                version = params.get('protocolVersion')
                if version not in {'2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'}:
                    version = '2025-06-18'
                result = {'protocolVersion': version, 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'ao_control', 'version': '1.0.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': available_tools(manifest)}
            elif method == 'tools/call':
                try:
                    value = invoke(manifest, params.get('name'), params.get('arguments', {}))
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
                except (ValueError, OSError, jobs.RunnerError, subprocess.TimeoutExpired) as exc:
                    result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
            else:
                raise ValueError('Unknown method')
            response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            response = {'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                        'error': {'code': -32602, 'message': str(exc)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    serve(jobs.read_json(Path(sys.argv[1])))
