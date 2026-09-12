"""Windows host integration, called only by explicit setup/operation actions."""
import base64
import ctypes
import os
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def run_maintenance_powershell(command: str, cwd: Path):
    """Run trusted maintenance commands without inheriting a PS7 module tree.

    RemoteSigned applies to this child only. Machine/user/group policies remain
    unchanged; onboarding must verify the maintenance script against its signed
    release before calling this helper.
    """
    system = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0'
    environment = os.environ.copy()
    environment['PSModulePath'] = str(system / 'Modules')
    return subprocess.run(
        [str(system / 'powershell.exe'), '-NoProfile', '-NonInteractive',
         '-ExecutionPolicy', 'RemoteSigned', '-Command', command],
        cwd=cwd, env=environment, capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]


def protect(text: str) -> str:
    return base64.b64encode(_dpapi(text.encode('utf-8'), True)).decode('ascii')


def unprotect(text: str) -> str:
    return _dpapi(base64.b64decode(text), False).decode('utf-8')


def _dpapi(raw: bytes, encrypt: bool) -> bytes:
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    result = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


def register_nightly(root: Path, executable: Path) -> None:
    """Task uses the current logged-on user so NAS credentials remain usable."""
    # A logged-off PC needs an administrator-provisioned service account, never saved passwords.
    ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', ns)
    task = ET.Element('Task', {'version': '1.2', 'xmlns': ns})
    triggers = ET.SubElement(task, 'Triggers')
    daily = ET.SubElement(triggers, 'CalendarTrigger')
    ET.SubElement(daily, 'StartBoundary').text = '2026-09-09T23:00:00+08:00'
    ET.SubElement(daily, 'Enabled').text = 'true'
    repetition = ET.SubElement(daily, 'Repetition')
    ET.SubElement(repetition, 'Interval').text = 'PT1H'
    ET.SubElement(repetition, 'Duration').text = 'PT23H'
    ET.SubElement(repetition, 'StopAtDurationEnd').text = 'false'
    ET.SubElement(ET.SubElement(daily, 'ScheduleByDay'), 'DaysInterval').text = '1'
    logon = ET.SubElement(triggers, 'LogonTrigger')
    ET.SubElement(logon, 'Enabled').text = 'true'
    user = subprocess.check_output(['whoami'], text=True, creationflags=subprocess.CREATE_NO_WINDOW).strip()
    ET.SubElement(logon, 'UserId').text = user
    principal = ET.SubElement(ET.SubElement(task, 'Principals'), 'Principal', {'id': 'Author'})
    ET.SubElement(principal, 'UserId').text = user
    ET.SubElement(principal, 'LogonType').text = 'InteractiveToken'
    ET.SubElement(principal, 'RunLevel').text = 'LeastPrivilege'
    settings = ET.SubElement(task, 'Settings')
    for name, value in [('MultipleInstancesPolicy', 'IgnoreNew'), ('StartWhenAvailable', 'true'),
                        ('DisallowStartIfOnBatteries', 'false'), ('StopIfGoingOnBatteries', 'false'),
                        ('WakeToRun', 'true'), ('ExecutionTimeLimit', 'PT2H')]:
        ET.SubElement(settings, name).text = value
    action = ET.SubElement(ET.SubElement(task, 'Actions', {'Context': 'Author'}), 'Exec')
    ET.SubElement(action, 'Command').text = str(executable)
    ET.SubElement(action, 'Arguments').text = f'--root "{root}" --nightly'
    ET.SubElement(action, 'WorkingDirectory').text = str(root)
    path = root / 'control/nightly-task.xml'
    ET.ElementTree(task).write(path, encoding='utf-16', xml_declaration=True)
    import hashlib
    suffix = hashlib.sha256(str(root).encode()).hexdigest()[:10]
    subprocess.run(['schtasks.exe', '/Create', '/TN', 'TianmingERP-Nightly-' + suffix,
                    '/XML', str(path), '/F'], check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
