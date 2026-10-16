"""Bundled runtime entry: graceful stop independent of the browser and GUI."""
import json
import os
from pathlib import Path
import sys
import threading
import time

# The dedicated system service uses -I and an immutable, approved script. Add
# only that script's release, never CWD/PYTHONPATH or the service user's site.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
    from app.core.home_rehearsal import admit_managed_server
    admit_managed_server(script=__file__, executable=sys.executable)
    from app.core.home_rehearsal import install_network_guard
    rehearsal = install_network_guard()
    # The web app reads the assistant's non-secret backup status from this root.
    # Keep the path available while consuming the process-control nonce here.
    control = Path(os.environ['TM_ERP_CONTROL'])
    from desktop_assistant.mac_published_runtime import selected
    if selected():
        from desktop_assistant.runtime_log import capture_console
        with capture_console(control):
            serve(control, rehearsal)
    else:
        serve(control, rehearsal)


def serve(control, rehearsal):
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
