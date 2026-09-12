from types import SimpleNamespace
from pathlib import Path
import tkinter as tk
import pytest
from desktop_assistant.gui import App


@pytest.fixture
def ui(tmp_path):
    windows=[]
    def create(current=None):
        root=tmp_path/str(len(windows));(root/'control').mkdir(parents=True)
        manager=SimpleNamespace(root=root,state={'current':current,'previous':None},manifest=lambda:{'version':'v-test'})
        window=tk.Tk();window.withdraw();windows.append(window)
        app=App(window,manager);window.update()
        return app,manager,window
    yield create
    for window in windows: window.destroy()


def buttons(app,key): return [b for k,b in app.action_buttons if k==key]


def test_empty_and_running_installations_have_separate_steps(ui):
    app,manager,window=ui()
    assert app.tabs.index(app.tabs.select())==1
    assert not app.more.winfo_manager()
    assert len(app.tabs.tabs())==2
    assert not buttons(app,'restore')[0].instate(['disabled'])
    assert all(b.instate(['disabled']) for b in buttons(app,'open'))
    assert buttons(app,'recover')[0].instate(['disabled'])
    live,_,_=ui('current')
    assert live.tabs.index(live.tabs.select())==0
    assert buttons(live,'restore')[0].instate(['disabled'])
    assert all(not b.instate(['disabled']) for b in buttons(live,'open'))
    assert '尚未' not in live.status.get()
    assert window.winfo_reqheight()<=740


def test_more_can_open_close_and_switch_back_without_business_action(ui):
    app,manager,window=ui('current')
    app.more_button.invoke();window.update()
    assert app.more.winfo_manager()=='pack' and not app.tabs.winfo_manager()
    assert window.winfo_reqheight()<=740
    app.more_button.invoke();window.update()
    assert not app.more.winfo_manager() and app.tabs.winfo_manager()=='pack'
    app.tabs.select(1);window.update()
    assert 'standby' in (manager.root/'control/ui-preferences.json').read_text()
    assert manager.state=={'current':'current','previous':None}


def test_busy_guard_and_final_button_eligibility(ui,monkeypatch):
    app,manager,window=ui('current');calls=[]
    class Thread:
        def __init__(self,target,**kwargs):self.target=target
        def start(self):self.target()
    monkeypatch.setattr('desktop_assistant.gui.threading.Thread',Thread)
    app.run('fixture',lambda:calls.append(1) or {'private_internal_key':'not for display'})
    app.run('duplicate',lambda:calls.append(2))
    assert calls==[1] and all(b.instate(['disabled']) for b in app.buttons)
    app.poll()
    assert not app.busy and 'private_internal_key' not in app.log.get()
    assert buttons(app,'restore')[0].instate(['disabled'])
    assert not buttons(app,'open')[0].instate(['disabled'])
    manager.state['operation']='migration_failed';app.refresh()
    assert not buttons(app,'recover')[0].instate(['disabled'])
    assert '继续上次未完成的更新' in app.status.get()


def test_restore_cancel_has_no_effect_and_success_explains_next_step(ui,monkeypatch):
    app,manager,window=ui();calls=[]
    monkeypatch.setattr('desktop_assistant.gui.filedialog.askopenfilename',lambda **kw:'')
    manager.restore=lambda *args:calls.append(args)
    app.restore();assert calls==[]
    monkeypatch.setattr('desktop_assistant.gui.filedialog.askopenfilename',lambda **kw:'fixture.tmbackup')
    monkeypatch.setattr('desktop_assistant.gui.simpledialog.askstring',lambda *a,**kw:None)
    app.restore();assert calls==[]
    monkeypatch.setattr('desktop_assistant.gui.simpledialog.askstring',lambda *a,**kw:'fixture-passphrase')
    manager.restore=lambda *a:{'data_time':'2026-09-12 23:00','started':False}
    results=[];app.run=lambda desc,action:results.append(action())
    app.restore()
    assert '第2步' in results[0] and '2026-09-12 23:00' in results[0]
