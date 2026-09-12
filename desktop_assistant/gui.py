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
        window.title('天明ERP助手')
        window.geometry('800x740')
        ttk.Style(window).configure('.', font=('Microsoft YaHei UI', 10))
        window.minsize(780, 720)
        box = ttk.Frame(window, padding=20)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='天明 ERP 助手', font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w')
        ttk.Label(box, text='先选择这台电脑的用途，再按页面提示操作。').pack(anchor='w', pady=(4, 8))
        self.status = tk.StringVar()
        ttk.Label(box, textvariable=self.status, wraplength=740).pack(anchor='w', pady=(0, 8))
        self.buttons = []
        self.action_buttons = []
        self.tabs = ttk.Notebook(box)
        self.tabs.pack(fill='x')
        factory = ttk.Frame(self.tabs, padding=12)
        standby = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(factory, text='  工厂电脑 · 日常使用  ')
        self.tabs.add(standby, text='  备用新电脑 · 故障接替  ')
        ttk.Label(factory, text='每天使用：点“打开ERP”。第一次使用：先设置每天备份，再做一次备份。',
                  wraplength=700).pack(anchor='w', pady=(0, 8))
        self.action_row(factory, 'open', '打开ERP', '开始处理订单、仓库和送货。', self.open_erp)
        self.action_row(factory, 'backup', '现在备份一次', '把当前数据和附件保存到共享盘，电脑坏了可用它恢复。', self.backup)
        self.action_row(factory, 'update', '更新ERP', '安装已发布的新版；助手会先备份，期间ERP会短暂停用。', self.update)
        self.action_row(factory, 'configure', '设置每天自动备份', '首次设置共享盘和恢复密码，以后每天晚上11点备份。', self.configure)
        ttk.Label(factory, text='自动备份时电脑须开机并保持账号登录，锁屏可以；恢复密码请另存一份。',
                  wraplength=700).pack(anchor='w', pady=(8, 0))
        ttk.Label(standby, text='工厂电脑损坏、不能继续使用时，按下面4步接替。\n先连接存放备份的共享盘，并准备好原来的恢复密码。',
                  wraplength=700).pack(anchor='w', pady=(0, 8))
        self.action_row(standby, 'restore', '1  从备份取回数据', '选择最新的完整备份文件，输入原恢复密码；仅用于空安装。', self.restore)
        self.action_row(standby, 'network', '2  设置本机访问地址', '填写新电脑的局域网地址，让其他电脑和手机能访问。', self.network)
        self.action_row(standby, 'open', '3  打开ERP并核对', '登录后先核对最近的订单、库存和附件，再开始录单。', self.open_erp)
        self.action_row(standby, 'configure', '4  设置每天自动备份', '新电脑需要重新设置；完成后到工厂电脑页做一次备份。', self.configure)
        ttk.Label(standby, text='仅准备备用机时，恢复后核对即可，不要与原电脑同时正式录单。\n数据截至所选备份的时间；接替后请另外检查打印机。',
                  wraplength=700).pack(anchor='w', pady=(8, 0))
        self.more_button = ttk.Button(box, text='遇到问题／其他设置 ▸', command=self.toggle_more)
        self.more_button.pack(anchor='w', pady=(12, 4))
        self.buttons.append(self.more_button)
        self.more = ttk.Frame(box)
        for key, title, hint, action in (
            ('recover', '继续上次未完成的更新', '更新断电或中断时使用，先检查再恢复。', self.recover_update),
            ('rollback', '退回上一个程序版本', '新版无法使用时使用；不会把业务数据退回旧日期。', self.rollback),
            ('import', '首次接入原ERP数据', '只在工厂原电脑第一次接入助手时使用。', self.import_old),
            ('network', '修改本机访问地址', '工厂电脑地址变化、手机无法访问时检查。', self.network),
            ('ai', '设置AI密钥（DeepSeek）', '启用AI库存解读，与备份恢复无关。', self.configure_ai),
        ):
            self.action_row(self.more, key, title, hint, action, compact=True)
        self.log = tk.StringVar(value='请选择上面的用途页签。这里会显示操作进度。')
        self.log_label = ttk.Label(box, textvariable=self.log, wraplength=740)
        self.log_label.pack(anchor='w', pady=(10, 0))
        try:
            saved = read_json(manager.root / 'control' / 'ui-preferences.json').get('computer_role')
        except (OSError, ValueError, AttributeError):
            saved = None
        self.tabs.select(1 if saved == 'standby' or (saved is None and not manager.state.get('current')) else 0)
        self.tabs.bind('<<NotebookTabChanged>>', self.role_changed)
        self.refresh()
        window.after(200, self.poll)

    def action_row(self, parent, key, title, hint, action, compact=False):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=2 if compact else 5)
        button = ttk.Button(row, text=title, width=26, command=action)
        button.pack(side='left', anchor='n')
        ttk.Label(row, text=hint, wraplength=475).pack(side='left', padx=(12, 0), anchor='w')
        self.buttons.append(button)
        self.action_buttons.append((key, button))

    def role_changed(self, event=None):
        role = 'standby' if self.tabs.index(self.tabs.select()) == 1 else 'factory'
        try:
            write_json(self.manager.root / 'control' / 'ui-preferences.json', {'computer_role': role})
        except OSError:
            self.log.set('本次用途已切换；暂时无法记住选择，下次打开时请重新选择。')
        if self.more.winfo_manager():
            self.toggle_more()

    def toggle_more(self):
        if self.more.winfo_manager():
            self.more.pack_forget()
            self.more_button.configure(text='遇到问题／其他设置 ▸')
            self.tabs.pack(fill='x', before=self.more_button)
        else:
            self.tabs.pack_forget()
            self.more.pack(fill='x', before=self.log_label)
            self.more_button.configure(text='收起其他设置，返回操作步骤 ◂')


    def refresh(self):
        state = self.manager.state
        current = bool(state.get('current'))
        version = self.manager.manifest()['version'] if current else '这台电脑尚未接入数据'
        last = state.get('last_backup_at')
        try:
            last = datetime.fromisoformat(last).astimezone(CN).strftime('%Y-%m-%d %H:%M') if last else '还没有成功备份，请先设置备份'
        except ValueError:
            last = str(last)
        text = f"本机：{version}    最近成功备份：{last}"
        if state.get('backup_error'):
            text += '\n上次备份未成功，请检查共享盘连接后再点“现在备份一次”。'
        if state.get('operation') in ('migration_running', 'migration_failed'):
            text += '\n上次更新未完成：展开“遇到问题／其他设置”，点“继续上次未完成的更新”。'
        if not current:
            text += '\n新电脑请选择备用新电脑页；原工厂电脑首次接入请展开其他设置。'
        self.status.set(text)
        for key, button in self.action_buttons:
            enabled = not self.busy
            if key in ('open', 'backup', 'update', 'configure', 'network', 'ai'):
                enabled = enabled and current
            elif key in ('restore', 'import'):
                enabled = enabled and not current
            elif key == 'recover':
                enabled = enabled and state.get('operation') in ('migration_running', 'migration_failed')
            elif key == 'rollback':
                enabled = enabled and bool(state.get('previous'))
            button.configure(state='normal' if enabled else 'disabled')

    def run(self, description, action):
        if self.busy:
            return
        self.busy = True
        self.log.set(description + '，请等待。')
        for button in self.buttons:
            button.configure(state='disabled')
        def work():
            try:
                result = action()
                self.events.put((True, result if isinstance(result, str) else '操作已完成。'))
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
            messagebox.showinfo('先设置备份', '请在工厂电脑页点击“设置每天自动备份”，选择共享盘并设置恢复密码。')
            return None

    def network(self):
        import ipaddress
        host = simpledialog.askstring('本机地址', '输入这台电脑的IPv4地址（Windows设置 → 网络和Internet → 网络属性中查看）。\n只有这台电脑使用ERP时，可填127.0.0.1。\n手机和其他电脑需要与这台电脑连接同一局域网。')
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
        nas = filedialog.askdirectory(title='选择用于保存ERP备份的共享文件夹（NAS）')
        if not nas:
            return
        if not nas.startswith(('\\\\', '//')):
            messagebox.showerror('使用网络路径', '请填写共享盘的网络地址，例如\\\\服务器\\共享\\ERP恢复；不要选择Z:这类盘符，可在文件选择窗口输入网络地址。')
            return
        password = simpledialog.askstring('恢复密码', '设置备份恢复密码（至少12个字符），请另存一份。\n这是取回备份用的密码，不是ERP登录密码：', show='*')
        if not password:
            return
        again = simpledialog.askstring('再次输入恢复密码', '再次输入刚才设置的恢复密码：', show='*')
        if password != again or len(password) < 12:
            messagebox.showerror('口令不匹配', '两次口令必须相同且不少于12个字符。')
            return
        if not getattr(sys, 'frozen', False):
            messagebox.showinfo('开发模式', '开发源码不注册计划任务，请使用安装后的助手。')
            return
        def save():
            write_json(self.manager.root / 'preferences.json', {'nas': nas, 'protected_password': protect(password)})
            register_nightly(self.manager.root, Path(sys.executable))
            return '已设置每天晚上11点自动备份。请到工厂电脑页点“现在备份一次”，确认共享盘可用。'
        self.run('保存备份设置', save)

    def configure_ai(self):
        from desktop_assistant.ai_config import save_deepseek_api_key

        key = simpledialog.askstring(
            '设置AI密钥',
            '粘贴从 DeepSeek 平台取得的专用密钥（不能使用 OpenAI 密钥）。\n密钥仅加密保存在本机，不进入ERP数据库、日志、发布包或NAS备份。',
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
                    save_deepseek_api_key(self.manager.root, key)
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
            messagebox.showinfo('没有发布包', '共享盘中还没有可用的更新包，请联系系统维护人员发布后再试。')
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
        path = filedialog.askopenfilename(title='选择共享盘中最新的完整备份（.tmbackup）', filetypes=[('ERP完整恢复包', '*.tmbackup')])
        if not path:
            return
        password = simpledialog.askstring('恢复密码', '输入工厂电脑备份时设置的恢复密码（不是ERP登录密码）：', show='*')
        if password:
            def recover():
                result = self.manager.restore(Path(path), password)
                return f"数据已取回，备份时间：{result['data_time']}。请继续第2步设置本机访问地址，再打开ERP核对。"
            self.run('正在取回备份数据', recover)

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
