"""Development-only local start. Public hosting requires a deployment and security review."""
import os, webbrowser, threading
import uvicorn
if __name__=='__main__':
    port=int(os.environ.get('VILO_PORT','8765'))
    print(f'VILO lokalnie: http://127.0.0.1:{port}/')
    print('Zamknij: Ctrl+C. Nie udostępniaj tego portu publicznie.')
    threading.Timer(1.5,lambda:webbrowser.open(f'http://127.0.0.1:{port}/')).start()
    uvicorn.run('server:app',host='127.0.0.1',port=port,workers=1)
