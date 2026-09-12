from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import webbrowser

from desktop_assistant.manager import CN, Manager
from desktop_assistant.storage import read_json, safe_name, write_json
from desktop_assistant.windows import protect, register_nightly, unprotect


def preferences(manager):
    return read_json(manager.root / 'preferences.json')


def nightly(manager):
    settings = preferences(manager)
    now = datetime.now(CN)
    due = now.replace(hour=23, minute=0, second=0, microsecond=0)
    if now < due:
        due -= timedelta(days=1)
    latest = manager.state.get('last_backup_at')
    if latest and datetime.fromisoformat(latest) >= due:
        with manager.lock():
            manager.start()
        return
    manager.backup(unprotect(settings['protected_password']), Path(settings['nas']))
    with manager.lock():
        manager.start()


class App:
    def __init__(self, window, manager):
        self.window, self.manager = window, manager
        self.events = queue.Queue()
        self.busy = False
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.title('天明ERP助手 · 更新、备份与恢复')
        window.geometry('760x680')
        window.minsize(700, 640)
        box = ttk.Frame(window, padding=24)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='天明 ERP 助手', font=('Microsoft YaHei UI', 21, 'bold')).pack(anchor='w')
        ttk.Label(box, text='程序打不开时，也可以在这里备份、回退和恢复。').pack(anchor='w', pady=(8, 18))
        self.status = tk.StringVar()
        ttk.Label(box, textvariable=self.status, wraplength=690).pack(anchor='w', pady=8)
        self.buttons = []
        for text, action in [('打开ERP', self.open_erp), ('检查并更新', self.update),
                             ('检查并恢复中断升级', self.recover_update),
                             ('回退到上一个版本', self.rollback), ('立即完整备份到NAS', self.backup),
                             ('从NAS恢复到本机空安装', self.restore), ('首次接入原ERP（只读复制）', self.import_old),
                             ('设置本机局域网访问地址', self.network),
                             ('设置AI密钥', self.configure_ai),
                             ('设置NAS与每天23点备份', self.configure)]:
            button = ttk.Button(box, text=text, command=action)
            button.pack(fill='x', pady=4)
            self.buttons.append(button)
        self.log = tk.StringVar(value='等待操作。首次安装请选择“恢复”或“接入原ERP”。')
        ttk.Label(box, textvariable=self.log, wraplength=690).pack(anchor='w', pady=12)
        ttk.Label(box, text='备份任务要求电脑开机且备份账号保持登录；锁屏可以，关机/注销不能执行。\n'
                  '缺失备份将在下次登录补做；失败每小时重试。换机后需重新设置备份口令和NAS访问。',
                  wraplength=690).pack(anchor='w', pady=8)
        self.refresh()
        window.after(200, self.poll)

    def refresh(self):
        state = self.manager.state
        version = self.manager.manifest()['version'] if state['current'] else '尚未恢复数据'
        self.status.set(f"当前版本：{version}\n最近成功备份：{state.get('last_backup_at', '尚无成功备份')}\n"
                        f"备份状态：{state.get('backup_error') or '无已记录错误'}"
                        + ('\n升级中断：请点击“检查并恢复中断升级”。' if state.get('operation') in
                           ('migration_running', 'migration_failed') else ''))

    def run(self, description, action):
        self.busy = True
        self.log.set(description + '，请等待。')
        for button in self.buttons:
            button.configure(state='disabled')
        def work():
            try:
                self.events.put((True, str(action() or '已完成')))
            except Exception as error:
                self.events.put((False, str(error)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            ok, message = self.events.get_nowait()
            self.busy = False
            self.log.set(('完成：' if ok else '未完成：') + message)
            for button in self.buttons:
                button.configure(state='normal')
            self.refresh()
        except queue.Empty:
            pass
        self.window.after(200, self.poll)

    def close(self):
        if self.busy:
            messagebox.showinfo('操作进行中', '请等待备份、更新或恢复完成后再关闭助手。')
        else:
            self.window.destroy()

    def settings(self):
        try:
            config = preferences(self.manager)
            return unprotect(config['protected_password']), Path(config['nas'])
        except Exception:
            messagebox.showinfo('先设置备份', '请先设置NAS路径和恢复口令。')
            return None

    def network(self):
        import ipaddress
        host = simpledialog.askstring('本机地址', '输入本机局域网IPv4地址；仅本机使用可填127.0.0.1。\n此设置启用现有局域网HTTP模式，不配置公网访问。')
        if not host:
            return
        try:
            ip = ipaddress.IPv4Address(host)
            if not (ip.is_private and not ip.is_unspecified and not ip.is_multicast):
                raise ValueError()
        except ValueError:
            messagebox.showerror('地址无效', '请输入本机私有IPv4地址。')
            return
        def save():
            with self.manager.lock():
                self.manager.stop()
                path = self.manager.root / 'shared/environment.json'
                config = read_json(path)
                port = config['ERP_PORT']
                config.update(ERP_BIND_HOST=host, ERP_PRODUCTION_TRANSPORT='lan_http',
                              ERP_HEALTH_URL=f'http://{host}:{port}/api/health',
                              ERP_BROWSER_URL=f'http://{host}:{port}/', ERP_SESSION_COOKIE_SECURE='false',
                              ERP_ALLOWED_ORIGINS=f'http://{host}:{port}', ERP_TRUSTED_HOSTS=f'{host},127.0.0.1,localhost')
                write_json(path, config)
            return '已保存本机地址；请打开ERP。其他设备访问可能还需管理员配置Windows防火墙。'
        self.run('设置访问地址', save)

    def configure(self):
        nas = filedialog.askdirectory(title='选择已有NAS完整恢复目录')
        if not nas:
            return
        if not nas.startswith(('\\\\', '//')):
            messagebox.showerror('使用网络路径', '请填写NAS的UNC共享路径，例如\\\\服务器\\共享\\ERP恢复；不使用依赖登录映射的Z盘。')
            return
        password = simpledialog.askstring('恢复口令', '设置至少12个字符的恢复口令，另存于电脑之外：', show='*')
        if not password:
            return
        again = simpledialog.askstring('确认口令', '再次输入恢复口令：', show='*')
        if password != again or len(password) < 12:
            messagebox.showerror('口令不匹配', '两次口令必须相同且不少于12个字符。')
            return
        if not getattr(sys, 'frozen', False):
            messagebox.showinfo('开发模式', '开发源码不注册计划任务，请使用安装后的助手。')
            return
        def save():
            write_json(self.manager.root / 'preferences.json', {'nas': nas, 'protected_password': protect(password)})
            register_nightly(self.manager.root, Path(sys.executable))
            return '23点任务已注册；请立即做一次完整备份验证NAS连接，并妥善保管恢复口令'
        self.run('保存备份设置', save)

    def configure_ai(self):
        from desktop_assistant.ai_config import save_openai_api_key

        key = simpledialog.askstring(
            '设置AI密钥',
            '粘贴从 OpenAI 安全创建入口取得的密钥。\n密钥仅加密保存在本机，不进入ERP数据库、日志、发布包或NAS备份。',
            show='*',
        )
        if not key:
            return
        again = simpledialog.askstring('确认AI密钥', '再次粘贴同一个密钥：', show='*')
        if key.strip() != (again or '').strip():
            messagebox.showerror('密钥不匹配', '两次输入的AI密钥不同，未保存。')
            return

        def save():
            with self.manager.lock():
                running = bool(self.manager._process())
                self.manager.stop()
                try:
                    save_openai_api_key(self.manager.root, key)
                finally:
                    if running:
                        self.manager.start()
            return 'AI密钥已加密保存；AI库存解读已启用，ERP已按原运行状态重新加载配置。'

        self.run('安全保存AI密钥', save)

    def open_erp(self):
        def start():
            with self.manager.lock():
                self.manager.start()
            config = read_json(self.manager.root / 'shared/environment.json')
            webbrowser.open(config.get('ERP_BROWSER_URL', 'http://127.0.0.1:' + config['ERP_PORT'] + '/'))
        self.run('启动ERP', start)

    def backup(self):
        settings = self.settings()
        if settings:
            self.run('暂停ERP写入并备份，完成后恢复服务', lambda: self.manager.backup(*settings))

    def update(self):
        settings = self.settings()
        if not settings:
            return
        # Git build publishes a signed release to the NAS releases directory.
        feed = settings[1] / 'releases/latest.json'
        if not feed.exists():
            messagebox.showinfo('没有发布包', 'NAS尚无已验证的新发布包；Git代码需要先构建为签名发布包。')
            return
        entry = read_json(feed)
        name = safe_name(entry['package'])
        if '/' in name:
            raise ValueError('发布文件名不合法')
        if messagebox.askyesno('更新', '将验证NAS发布包，先完整备份再更新；备份和更新期间ERP暂停使用。继续？'):
            self.run('验证、备份并更新', lambda: self.manager.update_from_feed(settings[1] / 'releases' / name, *settings))

    def rollback(self):
        settings = self.settings()
        if settings and messagebox.askyesno('回退程序', '将先备份当前数据，再回退程序；不回退业务数据库。继续？'):
            self.run('备份并回退程序', lambda: self.manager.rollback(*settings))

    def restore(self):
        path = filedialog.askopenfilename(title='选择NAS完整恢复包', filetypes=[('ERP完整恢复包', '*.tmbackup')])
        if not path:
            return
        password = simpledialog.askstring('恢复口令', '输入备份时设置的恢复口令：', show='*')
        if password:
            self.run('校验并恢复到空安装', lambda: self.manager.restore(Path(path), password))

    def recover_update(self):
        self.run('核对升级现场、备份和程序并恢复运行', self.manager.recover_interrupted_update)

    def import_old(self):
        from desktop_assistant.import_existing import import_existing
        source = filedialog.askdirectory(title='选择已正常停服的原ERP目录')
        package = self.manager.root / 'installer-release.zip'
        if source and messagebox.askyesno('首次接入', '请确认原ERP已正常停止。只复制数据到助手目录，原目录保留。继续？'):
            self.run('核对并复制原ERP', lambda: import_existing(self.manager, Path(source), package))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(os.environ['LOCALAPPDATA']) / 'TianmingERP')
    parser.add_argument('--nightly', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return
    resources = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    manager = Manager(args.root, (resources / 'release-public.pem').read_bytes())
    if args.nightly:
        try:
            nightly(manager)
        except Exception as error:
            write_json(manager.root / 'control/last-task-error.json', {'error': str(error), 'at': datetime.now(CN).isoformat()})
            raise SystemExit(1)
    else:
        window = tk.Tk()
        App(window, manager)
        window.mainloop()


if __name__ == '__main__':
    main()
