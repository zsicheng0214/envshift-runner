"""Block benchmark answer hosts during evaluation; preserve exact original hosts bytes."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys

HOSTS = ('github.com', 'www.github.com', 'raw.githubusercontent.com', 'api.github.com',
         'codeload.github.com', 'objects.githubusercontent.com', 'gist.github.com',
         'gist.githubusercontent.com')
MARKER = b'# envshift-terminal-answer-network\n'


def enable(hosts, backup):
    original = hosts.read_bytes()
    if backup.exists():
        raise RuntimeError('Refusing to overwrite hosts backup')
    backup.write_bytes(original)
    extra = MARKER + ''.join('127.0.0.1 %s\n::1 %s\n' % (h, h) for h in HOSTS).encode()
    hosts.write_bytes(original + b'\n' + extra)


def disable(hosts, backup):
    if backup.exists():
        hosts.write_bytes(backup.read_bytes())
        backup.unlink()


def verify(hosts):
    if MARKER not in hosts.read_bytes():
        raise RuntimeError('Answer network marker is missing')
    for host in HOSTS:
        addresses = {x[4][0] for x in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or not addresses <= {'127.0.0.1', '::1'}:
            raise RuntimeError('Answer host is not blocked: ' + host)
    print('ANSWER_NETWORK verified: %d hosts blocked' % len(HOSTS))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['on', 'off', 'check'])
    ap.add_argument('--backup', required=True)
    args = ap.parse_args()
    hosts = (Path(os.environ['SystemRoot']) / 'System32/drivers/etc/hosts'
             if os.name == 'nt' else Path('/etc/hosts'))
    backup = Path(args.backup)
    def flush():
        commands = ([['ipconfig', '/flushdns']] if os.name == 'nt' else
                    [['dscacheutil', '-flushcache'], ['killall', '-HUP', 'mDNSResponder']]
                    if sys.platform == 'darwin' else [])
        for command in commands:
            subprocess.run(command, capture_output=True, check=False)
    if args.mode == 'on':
        enable(hosts, backup)
        flush()
        verify(hosts)
    elif args.mode == 'off':
        disable(hosts, backup)
        flush()
        print('ANSWER_NETWORK restored')
    else:
        verify(hosts)
