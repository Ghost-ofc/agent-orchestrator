#!/usr/bin/env python3
"""Foreground entry point for a user service, with a private persistent browser capability."""
import argparse
import re
import secrets
import signal

import cli_agent_job as jobs
import dashboard_server


def service_token():
    path = jobs.state_home() / 'dashboard-service-token.json'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with jobs.record_lock(path):
        if path.exists():
            token = jobs.read_json(path).get('token')
            if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
                raise jobs.RunnerError('Invalid dashboard service token; refusing to replace it silently.')
            path.chmod(0o600)
            return token
        token = secrets.token_urlsafe(32)
        jobs.write_json(path, {'token': token})
        return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=44509)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('port must be between 0 and 65535')
    token = service_token()
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    return dashboard_server.serve(port=args.port, open_browser=False, token=token)


if __name__ == '__main__':
    raise SystemExit(main())
