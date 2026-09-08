"""Offline installer bootstrap; installs files, never starts or restores business data."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
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
        destination = install(Path(sys._MEIPASS), Path(parent) / 'TianmingERP')
        messagebox.showinfo('安装完成', '已建立桌面快捷方式。首次打开请选择接入原ERP或从NAS恢复。')
        subprocess.Popen([str(destination / 'TianmingERP-Assistant.exe'), '--root', str(destination)],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as error:
        messagebox.showerror('安装未完成', str(error))


if __name__ == '__main__':
    main()
