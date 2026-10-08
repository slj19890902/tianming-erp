from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import webbrowser

from desktop_assistant.manager import CN, Manager
from desktop_assistant.storage import read_json, safe_name, write_json
from desktop_assistant.windows import unprotect
from desktop_assistant.connectivity import browser_url, inspect as inspect_connectivity


def setup_defaults(manager):
    try:
        return read_json(manager.root / 'control/setup-defaults.json')
    except (OSError, ValueError):
        return {}


def preferences(manager):
    return read_json(manager.root / 'preferences.json')


def nightly(manager):
    if not manager.state.get('current') or manager.state.get('manual_stop') or manager.state.get('onboarding_pending'):
        return
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
        self.on_success = None
        self.connection_events = queue.Queue()
        self.connection_check_running = False
        self.connection_status = '网页访问：待检查'
        self.next_connection_check = 0.0
        window.report_callback_exception = self.report_callback_exception
        window.protocol('WM_DELETE_WINDOW', self.close)
        from app.version import APP_VERSION
        window.title('天明ERP助手 · 助手 ' + APP_VERSION)
        window.geometry('800x740')
        ttk.Style(window).configure('.', font=('Microsoft YaHei UI', 10))
        window.minsize(780, 720)
        box = ttk.Frame(window, padding=20)
        box.pack(fill='both', expand=True)
        self.content = box
        self.backup_panel = None
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
        ttk.Label(factory, text='第一次：设置备份 → 首次接入。以后：直接启动、备份或更新ERP。',
                  wraplength=700).pack(anchor='w', pady=(0, 8))
        self.action_row(factory, 'open', '启动并打开ERP', 'ERP停止时自动启动；关闭助手窗口不会停止ERP。', self.open_erp)
        self.action_row(factory, 'pause', '备份后停止ERP', '准备维护或关机时使用；暂停期间所有员工不能录单。', self.pause_erp)
        self.action_row(factory, 'backup', '现在备份一次', '把当前数据和附件保存到共享盘，电脑坏了可用它恢复。', self.backup)
        self.action_row(factory, 'update', '更新ERP', '从共享盘同步已发布新版，自动备份、停服、更新和启动。', self.update)
        self.action_row(factory, 'configure', '设置每天自动备份', '首次设置共享盘和恢复密码，以后每天晚上11点备份。', self.configure)
        self.action_row(factory, 'import', '首次接入并启用', '仅第一次：选择原ERP整个文件夹，自动检查、停服、复制和备份。', self.import_old, compact=True)
        ttk.Label(factory, text='自动备份时保持账号登录和NAS连接，锁屏可以；恢复密码请另存一份。',
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
            ('repair_access', '修复网页入口', '后台正常但网页打不开时，恢复已配置私网转发；不关闭防火墙。', self.repair_access),
            ('recover', '继续上次未完成的更新', '更新断电或中断时使用，先检查再恢复。', self.recover_update),
            ('rollback', '退回上一个程序版本', '新版无法使用时使用；不会把业务数据退回旧日期。', self.rollback),
            ('finish', '完成首次接入', '接入已复制但备份中断时，修复共享盘后继续。', self.finish_import),
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
        running = bool(self.manager._process()) if current and hasattr(self.manager, '_process') else False
        service = ('进程存在' if running else '已停止') if current else '尚未接入'
        connection = ('操作进行中，连接状态待复核' if self.busy else
                      self.connection_status if running else '后台已停止 · 网页不可用')
        text = f"ERP程序：{version}    状态：{service}\n{connection}\n最近成功备份：{last}"
        if state.get('backup_error'):
            text += '\n上次备份未成功，请检查共享盘连接后再点“现在备份一次”。'
        if state.get('operation') in ('migration_running', 'migration_failed'):
            text += '\n上次更新未完成：展开“遇到问题／其他设置”，点“继续上次未完成的更新”。'
        if not current:
            text += '\n原工厂电脑：先设置备份，再点“首次接入并启用”；备用机请选择另一页。'
        if state.get('onboarding_pending'):
            text += '\n首次接入尚未完成完整备份，请展开其他设置继续完成。'
        self.status.set(text)
        for key, button in self.action_buttons:
            enabled = not self.busy
            if key in ('open', 'backup', 'update', 'network', 'ai', 'pause', 'repair_access'):
                enabled = enabled and current and not state.get('onboarding_pending')
                if key == 'pause':
                    enabled = enabled and running
            elif key == 'finish':
                enabled = enabled and bool(state.get('onboarding_pending'))
            elif key in ('restore', 'import'):
                enabled = enabled and not current
            elif key == 'recover':
                enabled = enabled and state.get('operation') in ('migration_running', 'migration_failed')
            elif key == 'rollback':
                enabled = enabled and bool(state.get('previous'))
            button.configure(state='normal' if enabled else 'disabled')

    def run(self, description, action, on_success=None):
        if self.busy:
            return
        self.busy = True
        self.on_success = on_success
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
            message = self.connection_events.get_nowait()
            self.connection_status = message
            self.connection_check_running = False
            self.next_connection_check = time.monotonic() + 15
            self.refresh()
        except queue.Empty:
            pass
        if not self.busy and not self.connection_check_running and time.monotonic() >= self.next_connection_check:
            self.connection_check_running = True
            def check():
                try:
                    config = read_json(self.manager.root / 'shared/environment.json')
                    result = inspect_connectivity(config, bool(self.manager._process()))
                    message = ' · '.join(result)
                except Exception:
                    message = '连接配置待核对：未能完成网页检查'
                self.connection_events.put(message)
            threading.Thread(target=check, daemon=True).start()
        try:
            ok, message = self.events.get_nowait()
            self.busy = False
            completed, self.on_success = self.on_success, None
            if ok and completed:
                completed()
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

    def report_callback_exception(self, error_type, error, trace):
        # Windowed executables have no console. Surface callback failures while
        # keeping passwords, API keys and exception messages out of diagnostics.
        import traceback
        kind = error_type.__name__
        self.log.set(f'界面操作未完成（{kind}），请重试；仍失败请联系维护人员。')
        try:
            frames = traceback.extract_tb(trace)
            write_json(self.manager.root / 'control/last-ui-error.json', {
                'type': kind, 'at': datetime.now(CN).isoformat(),
                'frames': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno} for f in frames],
            })
        except Exception:
            pass

    def configure(self):
        if self.busy:
            return
        if self.backup_panel is not None:
            self.backup_directory_entry.focus_set()
            return
        from desktop_assistant.backup_settings import load_preferences
        saved = load_preferences(self.manager.root)
        self.tabs.pack_forget()
        self.more.pack_forget()
        self.more_button.pack_forget()
        panel = self.backup_panel = ttk.Frame(self.content, padding=12)
        panel.pack(fill='x', before=self.log_label)
        ttk.Label(panel, text='设置每天自动备份', font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w', pady=(0, 10))
        ttk.Label(panel, text='每天晚上11点备份；电脑保持开机、账号登录及NAS连接。', wraplength=700).pack(anchor='w')
        ttk.Label(panel, text='NAS备份文件夹', padding=(0, 12, 0, 4)).pack(anchor='w')
        self.backup_directory = tk.StringVar(value=saved.get('nas') or setup_defaults(self.manager).get('backup_dir', ''))
        self.backup_directory_entry = ttk.Entry(panel, textvariable=self.backup_directory)
        self.backup_directory_entry.pack(fill='x')
        ttk.Label(panel, text='可直接使用上面的目录，也可粘贴共享文件夹的完整路径。', wraplength=700).pack(anchor='w', pady=(4, 10))
        self.backup_password = tk.StringVar()
        self.backup_confirmation = tk.StringVar()
        self.backup_inputs = [self.backup_directory_entry]
        for label, variable in [('恢复密码（至少12个字符）', self.backup_password), ('再次输入恢复密码', self.backup_confirmation)]:
            ttk.Label(panel, text=label).pack(anchor='w')
            entry = ttk.Entry(panel, textvariable=variable, show='*')
            entry.pack(fill='x', pady=(4, 10))
            self.backup_inputs.append(entry)
        hint = '已设置恢复密码：两栏留空可继续使用原密码。' if saved.get('protected_password') else '恢复密码用于取回备份，请在电脑以外另存；不是员工登录密码。'
        ttk.Label(panel, text=hint, wraplength=700).pack(anchor='w', pady=(0, 10))
        actions = ttk.Frame(panel)
        actions.pack(fill='x')
        self.backup_save_button = ttk.Button(actions, text='保存并启用自动备份', command=self.save_backup_settings)
        self.backup_save_button.pack(side='left')
        self.backup_cancel_button = ttk.Button(actions, text='返回', command=self.close_backup_settings)
        self.backup_cancel_button.pack(side='left', padx=12)
        self.buttons.extend([self.backup_save_button, self.backup_cancel_button])
        self.log.set('请确认备份文件夹，填写恢复密码后点“保存并启用自动备份”。')
        self.backup_directory_entry.focus_set()

    def close_backup_settings(self):
        if self.busy or self.backup_panel is None:
            return
        self.backup_password.set('')
        self.backup_confirmation.set('')
        for button in (self.backup_save_button, self.backup_cancel_button):
            self.buttons.remove(button)
        self.backup_panel.destroy()
        self.backup_panel = None
        self.more_button.configure(text='遇到问题／其他设置 ▸')
        self.more_button.pack(anchor='w', pady=(12, 4), before=self.log_label)
        self.tabs.pack(fill='x', before=self.more_button)
        self.log.set('已返回；未保存的设置不会生效。')

    def save_backup_settings(self):
        if self.busy:
            return
        from desktop_assistant.backup_settings import save_backup_settings
        directory, password, again = self.backup_directory.get(), self.backup_password.get(), self.backup_confirmation.get()
        if not directory.strip():
            self.log.set('请填写NAS备份文件夹。')
            return
        if (password or again) and (password != again or len(password) < 12):
            self.log.set('两次恢复密码必须相同，且至少12个字符。')
            return
        if not getattr(sys, 'frozen', False):
            self.log.set('开发模式不注册计划任务，请使用安装后的助手。')
            return
        self.run('正在检查备份文件夹并保存设置',
                 lambda: save_backup_settings(self.manager, directory, password, again, Path(sys.executable)),
                 on_success=self.close_backup_settings)

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

    def pause_erp(self):
        settings = self.settings()
        if settings and messagebox.askyesno('备份后停服', '请先让员工保存正在编辑的单据。助手将完整备份后停止ERP；夜间任务不会自动重新启动。继续？'):
            self.run('备份并停止ERP', lambda: self.manager.pause_after_backup(*settings))

    def finish_import(self):
        from desktop_assistant.onboarding import finish_onboarding
        settings = self.settings()
        if settings:
            self.run('完成首次备份并启动', lambda: finish_onboarding(self.manager, *settings))

    def open_erp(self):
        def start():
            self.manager.resume()
            config = read_json(self.manager.root / 'shared/environment.json')
            backend, page = inspect_connectivity(config, bool(self.manager._process()))
            if page != '网页可访问':
                raise ValueError(backend + '；' + page)
            webbrowser.open(browser_url(config))
        self.run('启动ERP', start)

    def repair_access(self):
        def repair():
            from desktop_assistant.connectivity import repair_lan_forward
            with self.manager.lock():
                result = repair_lan_forward(read_json(self.manager.root / 'shared/environment.json'))
            self.next_connection_check = 0.0
            return result
        self.run('修复已配置网页入口', repair)

    def backup(self):
        settings = self.settings()
        if settings:
            self.run('暂停ERP写入并备份，完成后恢复服务', lambda: self.manager.backup(*settings))

    def update(self):
        settings = self.settings()
        if not settings:
            return
        # Git build publishes a signed release to the NAS releases directory.
        config = preferences(self.manager)
        release_dir = Path(config.get('release_feed') or setup_defaults(self.manager).get('release_feed') or settings[1] / 'releases')
        feed = release_dir / 'latest.json'
        if not feed.exists():
            selected = filedialog.askdirectory(title='选择ERP发布更新文件夹（里面有latest.json；与备份文件夹可以不同）')
            if not selected:
                return
            release_dir = Path(selected)
            feed = release_dir / 'latest.json'
            if not feed.is_file():
                messagebox.showinfo('没有发布包', '该文件夹没有latest.json，请选择维护人员提供的ERP更新目录。')
                return
            config['release_feed'] = str(release_dir)
            write_json(self.manager.root / 'preferences.json', config)
        entry = read_json(feed)
        name = safe_name(entry['package'])
        if '/' in name:
            raise ValueError('发布文件名不合法')
        if messagebox.askyesno('更新', '将验证NAS发布包，先完整备份再更新；备份和更新期间ERP暂停使用。继续？'):
            self.run('验证、备份并更新', lambda: self.manager.update_from_feed(release_dir / name, *settings))

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
        from desktop_assistant.onboarding import onboard
        package = self.manager.root / 'installer-release.zip'
        if not package.is_file():
            messagebox.showerror('安装包不完整',
                                 f'当前助手目录：{self.manager.root}\n缺少ERP程序安装包。请运行最新版完整安装器，选择此目录修复安装，再进行首次接入。')
            return
        settings = self.settings()
        if not settings:
            return
        source = filedialog.askdirectory(title='选择原ERP整个文件夹（里面有data文件夹，不要选择data本身）', initialdir=setup_defaults(self.manager).get('source_root'))
        if source and messagebox.askyesno('首次接入', '请先让员工保存单据。助手会核对原ERP、暂停服务、复制数据、保存完整备份并启动新入口。以后通过本助手维护，原目录保留。继续？'):
            self.run('核对并复制原ERP', lambda: onboard(self.manager, Path(source), package, *settings))


def startup_root(explicit=None):
    if explicit is not None:
        return Path(explicit)
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(os.environ['LOCALAPPDATA']) / 'TianmingERP'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path)
    parser.add_argument('--nightly', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--nas-probe-path', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.nas_probe_path is not None:
        from desktop_assistant.nas_probe import probe
        try:
            raise SystemExit(probe(args.nas_probe_path))
        except (OSError, ValueError):
            raise SystemExit(2)
    if args.self_test:
        import tempfile
        from types import SimpleNamespace
        from desktop_assistant.onboarding import self_test_powershell
        from desktop_assistant.cleanup import remove_owned_tree
        from desktop_assistant.retention import cleanup_releases
        assert callable(cleanup_releases)
        assert browser_url({'ERP_PORT': '18000', 'ERP_BROWSER_URL': 'https://example.com/',
                            'ERP_LAN_HTTP_ORIGIN': 'http://192.168.3.80:8000'}) == 'http://192.168.3.80:8000/'
        if getattr(sys, 'frozen', False):
            assert startup_root() == Path(sys.executable).resolve().parent
        self_test_powershell()
        with tempfile.TemporaryDirectory(prefix='tm-assistant-ui-check-') as temp:
            root = Path(temp)
            (root / 'control').mkdir()
            copies = root / 'self-test-copies'
            copies.mkdir()
            (copies / 'disposable.txt').write_text('synthetic')
            assert remove_owned_tree(copies, root) > 0 and not copies.exists()
            probe = SimpleNamespace(root=root, state={'current': None, 'previous': None})
            window = tk.Tk()
            window.withdraw()
            try:
                app = App(window, probe)
                next(b for key, b in app.action_buttons if key == 'configure').invoke()
                window.update()
                assert app.backup_panel is not None and app.backup_panel.winfo_manager() == 'pack'
                assert not (root / 'preferences.json').exists()
                app.close_backup_settings()
            finally:
                window.destroy()
        return
    resources = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    manager = Manager(startup_root(args.root), (resources / 'release-public.pem').read_bytes())
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
