"""Offline installer bootstrap; installs files, never starts or restores business data."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import tkinter as tk
from tkinter import filedialog, messagebox


def install(payload: Path, destination: Path):
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('安装目录必须为空；更新请使用现有助手，不能覆盖旧安装')
    destination.mkdir(parents=True, exist_ok=True)
    # Restrict secrets and recovery plaintext staging to this Windows user and SYSTEM.
    sid = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW).strip().split(',')[-1].strip('"')
    subprocess.run(['icacls', str(destination), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F',
                    '*S-1-5-18:(OI)(CI)F'], check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    shutil.copyfile(payload / 'TianmingERP-Assistant.exe', destination / 'TianmingERP-Assistant.exe')
    shutil.copyfile(payload / 'release.zip', destination / 'installer-release.zip')
    # Shortcut target contains no shell command interpolation.
    target = str(destination / 'TianmingERP-Assistant.exe').replace("'", "''")
    root = str(destination).replace("'", "''")
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
              "[IO.Path]::Combine([Environment]::GetFolderPath('Desktop'),'天明ERP助手.lnk'));"
              f"$s.TargetPath='{target}';$s.Arguments='--root \"{root}\"';$s.WorkingDirectory='{root}';$s.Save()")
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                   check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return destination


def update_launcher(payload: Path, destination: Path):
    """Refresh bootstrap programs only; never touch shared data or the running ERP."""
    destination = destination.resolve()
    assistant = destination / 'TianmingERP-Assistant.exe'
    if (not assistant.is_file() or not (destination / 'installer-release.zip').is_file()
            or assistant.is_symlink() or destination.is_junction()):
        raise ValueError('请选择已有天明ERP助手安装目录，不能覆盖其他程序目录')
    temporary = destination / ('assistant-update-' + uuid.uuid4().hex + '.tmp')
    next_package = destination / ('package-update-' + uuid.uuid4().hex + '.tmp')
    try:
        shutil.copyfile(payload / 'TianmingERP-Assistant.exe', temporary)
        shutil.copyfile(payload / 'release.zip', next_package)
        os.replace(temporary, assistant)
        os.replace(next_package, destination / 'installer-release.zip')
    except PermissionError:
        raise ValueError('请先关闭旧版天明ERP助手窗口，再更新助手；ERP服务无需停止') from None
    finally:
        temporary.unlink(missing_ok=True)
        next_package.unlink(missing_ok=True)
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return
    root = tk.Tk()
    root.withdraw()
    if not messagebox.askokcancel('安装天明ERP助手', '安装离线ERP运行环境及独立更新恢复助手。\n已有ERP请保留原目录，安装完成后选择接入或NAS恢复。'):
        return
    parent = filedialog.askdirectory(title='选择安装位置（将在其中新建TianmingERP目录）',
                                    initialdir=os.environ['LOCALAPPDATA'])
    if not parent:
        return
    try:
        destination = Path(parent) / 'TianmingERP'
        if destination.exists() and any(destination.iterdir()):
            if not messagebox.askyesno('更新已有助手', '此处已有安装。是否更新助手和离线程序包？\n业务数据、原配置和正在运行的ERP保持不变。请先关闭旧助手窗口。'):
                return
            update_launcher(Path(sys._MEIPASS), destination)
        else:
            destination = install(Path(sys._MEIPASS), destination)
        messagebox.showinfo('安装完成', '助手已就绪。已有系统请点击检查更新；首次使用请选择接入原ERP或从NAS恢复。')
        subprocess.Popen([str(destination / 'TianmingERP-Assistant.exe'), '--root', str(destination)],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as error:
        messagebox.showerror('安装未完成', str(error))


if __name__ == '__main__':
    main()
