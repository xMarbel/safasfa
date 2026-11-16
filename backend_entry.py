"""Entry point for VILO's private desktop backend.

Electron launches this module as a child process. Unlike backend/start.py,
this module must never open a browser window by itself.
"""
import os
import uvicorn
from server import app


def main():
    port = int(os.environ.get('VILO_PORT', '8765'))
    if not 1024 <= port <= 65535:
        raise ValueError('Nieprawidłowy port serwera VILO')
    uvicorn.run(app, host='127.0.0.1', port=port, workers=1,
                access_log=False, log_level='warning')


if __name__ == '__main__':
    main()
