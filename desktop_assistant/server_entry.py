"""Bundled runtime entry: graceful stop independent of the browser and GUI."""
import json
import os
from pathlib import Path
import threading
import time

import uvicorn


def main():
    control = Path(os.environ.pop('TM_ERP_CONTROL'))
    nonce = os.environ.pop('TM_ERP_NONCE')
    server = uvicorn.Server(uvicorn.Config(
        'app.main:app', host=os.environ['ERP_BIND_HOST'], port=int(os.environ['ERP_PORT']),
        workers=1, timeout_graceful_shutdown=45,
    ))

    def watch():
        while not server.should_exit:
            if server.started:
                (control / (nonce + '.ready')).write_text(str(os.getpid()), encoding='ascii')
            if (control / (nonce + '.stop')).exists():
                server.should_exit = True
                return
            time.sleep(.5)

    threading.Thread(target=watch, daemon=True).start()
    server.run()


if __name__ == '__main__':
    main()
