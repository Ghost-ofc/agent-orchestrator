"""Linux desktop folder chooser. Only a deliberate authenticated panel action invokes this.

The web server stays stdlib-only; this short-lived system-Python helper uses the desktop's
PyGObject/Gio binding to XDG Desktop Portal. It returns a local path, never browser uploads.
Portal contract: https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.FileChooser.html
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys


class FolderPickerError(Exception):
    pass


def pick_folder(initial: Path, title: str) -> str | None:
    interpreter = '/usr/bin/python3' if Path('/usr/bin/python3').is_file() else sys.executable
    try:
        result = subprocess.run([interpreter, str(Path(__file__).resolve()), '--initial', str(initial), '--title', title],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=190, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FolderPickerError('No se pudo abrir el selector del sistema. Puedes escribir la ruta manualmente.') from exc
    if result.returncode:
        raise FolderPickerError('El selector del sistema no está disponible. Puedes escribir la ruta manualmente.')
    try:
        response = json.loads(result.stdout)
        if response == {'cancelled': True}:
            return None
        path = Path(response['path'])
        if not path.is_absolute() or not path.is_dir():
            raise ValueError('Not a local directory')
        return str(path.resolve())
    except (ValueError, KeyError, TypeError) as exc:
        raise FolderPickerError('El selector no devolvió una carpeta local válida.') from exc


def portal_folder(initial: str, title: str, timeout: int = 170) -> dict:
    import gi
    gi.require_version('Gio', '2.0')
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    token = 'ao_' + secrets.token_hex(12)
    sender = bus.get_unique_name().lstrip(':').replace('.', '_')
    request = f'/org/freedesktop/portal/desktop/request/{sender}/{token}'
    loop = GLib.MainLoop()
    answer = {'cancelled': True}
    pending = []
    completed = False

    def receive(_connection, _sender, path, _interface, _signal, parameters, _data):
        if path != request:
            if path.endswith('/' + token):
                pending.append((path, parameters))
            return
        finish(parameters)

    def finish(parameters):
        nonlocal answer, completed
        completed = True
        code, values = parameters.unpack()
        if code == 0:
            uris = values.get('uris', [])
            if len(uris) == 1:
                local = Gio.File.new_for_uri(uris[0]).get_path()
                if local and Path(local).is_dir():
                    answer = {'path': local}
                else:
                    answer = {'error': 'Not a local directory'}
        elif code != 1:
            answer = {'error': 'Desktop chooser failed'}
        loop.quit()

    def expire():
        nonlocal timer
        timer = None
        try:
            bus.call_sync('org.freedesktop.portal.Desktop', request, 'org.freedesktop.portal.Request',
                          'Close', None, None, Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error:
            pass
        loop.quit()
        return False

    subscription = bus.signal_subscribe('org.freedesktop.portal.Desktop', 'org.freedesktop.portal.Request',
                                        'Response', None, None, Gio.DBusSignalFlags.NONE, receive, None)
    options = {'handle_token': GLib.Variant('s', token), 'directory': GLib.Variant('b', True),
               'multiple': GLib.Variant('b', False), 'modal': GLib.Variant('b', False),
               'accept_label': GLib.Variant('s', 'Elegir carpeta'),
               'current_folder': GLib.Variant('ay', os.fsencode(initial) + b'\0')}
    timer = None
    try:
        reply = bus.call_sync('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop',
                              'org.freedesktop.portal.FileChooser', 'OpenFile',
                              GLib.Variant('(ssa{sv})', ('', title, options)), GLib.VariantType('(o)'),
                              Gio.DBusCallFlags.NONE, 15000, None)
        request = reply.unpack()[0]
        for path, parameters in pending:
            if path == request:
                finish(parameters)
                return answer
        timer = GLib.timeout_add_seconds(timeout, expire)
        if not completed:
            loop.run()
        return answer
    finally:
        bus.signal_unsubscribe(subscription)
        if timer is not None:
            GLib.source_remove(timer)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--initial', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--timeout', type=int, default=170)
    args = parser.parse_args()
    try:
        response = portal_folder(args.initial, args.title, args.timeout)
        if 'error' in response:
            raise FolderPickerError(response['error'])
        print(json.dumps(response))
    except Exception as exc:
        print(f'Folder chooser unavailable: {type(exc).__name__}', file=sys.stderr)
        sys.exit(1)
