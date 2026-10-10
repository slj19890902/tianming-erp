"""Bundled runtime entry: graceful stop independent of the browser and GUI."""
import json
import os
from pathlib import Path
import threading
import time

import uvicorn


def configure_managed_drawing_storage(control):
    """Pin missing drawing storage to the same managed shared data tree."""
    if os.environ.get('ERP_FILE_STORAGE_DIR'):
        return
    shared = control.resolve().parent / 'shared'
    configured_db = os.environ.get('ERP_DATABASE_PATH')
    if not configured_db or Path(configured_db).resolve() != (shared / 'data/carton_erp.sqlite3').resolve():
        raise ValueError('受管图纸存储与数据库目录不一致，拒绝启动')
    os.environ['ERP_FILE_STORAGE_DIR'] = str((shared / 'data/private_uploads').resolve())


def main():
    from app.core.home_rehearsal import install_network_guard
    rehearsal = install_network_guard()
    # The web app reads the assistant's non-secret backup status from this root.
    # Keep the path available while consuming the process-control nonce here.
    control = Path(os.environ['TM_ERP_CONTROL'])
    configure_managed_drawing_storage(control)
    from desktop_assistant.delivery_dispatch_contract import require_managed_dispatch_activation
    require_managed_dispatch_activation(os.environ.get('ERP_DATABASE_PATH'), control)
    nonce = os.environ.pop('TM_ERP_NONCE')
    server = uvicorn.Server(uvicorn.Config(
        'app.main:app', host=os.environ['ERP_BIND_HOST'], port=int(os.environ['ERP_PORT']),
        workers=1, timeout_graceful_shutdown=45,
        loop="asyncio" if rehearsal else "auto",
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
