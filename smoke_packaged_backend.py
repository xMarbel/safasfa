"""Smoke-test the actual frozen Windows backend on a GitHub Actions Windows runner."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen

root = Path(__file__).resolve().parents[1]
exe = root / 'backend-dist' / 'vilo-server' / 'vilo-server.exe'
assert exe.is_file(), f'Nie znaleziono: {exe}'
with socket.socket() as test_socket:
    test_socket.bind(('127.0.0.1', 0))
    port = test_socket.getsockname()[1]

with tempfile.TemporaryDirectory(prefix='vilo-packaged-test-') as data_dir:
    env = dict(os.environ, VILO_PORT=str(port), VILO_DATA_DIR=data_dir)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen([str(exe)], cwd=exe.parent, env=env,
                                stdout=log, stderr=subprocess.STDOUT,
                                creationflags=flags)
        try:
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError('Zamrożony backend zakończył pracę (exit=%s)' % proc.returncode)
                try:
                    with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1) as res:
                        assert json.load(res)['status'] == 'ok'
                    with urlopen(f'http://127.0.0.1:{port}/', timeout=1) as res:
                        html = res.read().decode()
                        assert 'VILO' in html and '/static/app.js' in html
                    with urlopen(f'http://127.0.0.1:{port}/static/app.js', timeout=1) as res:
                        assert len(res.read()) > 10000
                    with urlopen(f'http://127.0.0.1:{port}/static/style.css', timeout=1) as res:
                        assert len(res.read()) > 10000
                    print('OK: frozen backend, health, index.html, app.js, style.css')
                    break
                except (OSError, TimeoutError, ConnectionError):
                    time.sleep(0.3)
            else:
                raise RuntimeError('Backend nie odpowiedział w ciągu 35 sekund')
        except Exception:
            log.seek(0)
            print(log.read().decode('utf8', errors='replace')[-7000:])
            raise
        finally:
            proc.terminate()
            try: proc.wait(timeout=8)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=8)
