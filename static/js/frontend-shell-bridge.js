(() => {
  'use strict';
  if (window.parent === window || new URLSearchParams(location.search).get('frontend_shell') !== '1') return;
  // This opt-in belongs to the current outer shell. Apply layout before the
  // business app paints; connection/authentication still gate every command.
  const unifiedLayout = new URLSearchParams(location.search).get('unified_navigation') === '1';
  if (unifiedLayout) document.documentElement.classList.add('frontend-shell-layout');
  window.erpWorkspaceActive = false;
  window.ERPFrontendShell = {
    install(vm) {
      let connected = false;
      let unified = false, activeSurface = false, commandBusy = false;
      const commands = new Map();
      const entryRequests = new Map();
      const ready = () => !!vm.user && !vm.user.must_change_password && !window.erpCheckingSession;
      let lastNavigation = '';
      const notify = data => {
        const snapshot = JSON.stringify(data);
        if (snapshot === lastNavigation) return;
        lastNavigation = snapshot;
        window.parent.postMessage(data, location.origin);
      };
      const publish = () => {
        if (!activeSurface || document.hidden) return;
        if (!connected || !ready()) {
          document.documentElement.classList.remove('frontend-shell-connected','frontend-shell-unified');
          if (connected && vm.user) notify({type:'tianming-formal-navigation-v1',menus:vm.user.must_change_password ? [] : (vm.menus || []).map(item => ({key:String(item.key),label:String(item.label)})),ready:false});
          if (connected && !vm.user && !window.erpCheckingSession) {
            if (window.erpSessionUnavailable) {
              notify({type:'tianming-formal-navigation-v1',menus:[],ready:false,sessionUnavailable:true});
              return;
            }
            notify({type:'tianming-formal-navigation-v1',menus:[],active:'',authenticated:false});
          }
          return;
        }
        const menus = (vm.menus || []).map(item => ({key:String(item.key),label:String(item.label)}));
        const active = (vm.menus || []).find(item => vm.isMenuActive(item.key))?.key || '';
        document.documentElement.classList.add('frontend-shell-connected');
        document.documentElement.classList.toggle('frontend-shell-unified', unified);
        const draftState = window.ERPFrontendReliability?.state?.();
        const draftOpen = draftState ? !!(draftState.dirty || draftState.saving || draftState.uncertain) : !!(vm.modal?.type || vm.productionEntry);
        const shellUi = unified ? window.ERPUnifiedNavigation.describe(vm) : null;
        notify({type:'tianming-formal-navigation-v1',menus,active,page:vm.activePage,ready:true,draftOpen,shellUi});
      };
      // These three entries only open the original workflow. No data, save,
      // email sync, mutation method, or arbitrary method name crosses the bridge.
      const openEntry = async data => {
        const {action, requestId} = data;
        if (!['new','import','email'].includes(action) || typeof requestId !== 'string'
          || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(requestId)) return;
        const reply = status => window.parent.postMessage({type:'tianming-formal-order-entry-result-v1',action,requestId,status}, location.origin);
        const previous = entryRequests.get(requestId);
        if (previous) {
          if (previous.action !== action) reply('denied');
          else if (previous.status) reply(previous.status);
          return;
        }
        const attempt = {action,status:null};
        entryRequests.set(requestId, attempt);
        const finish = status => {attempt.status=status;reply(status);publish();};
        if (!connected || !ready() || !vm.canCreateOrders
          || !vm.hasPermission('orders.view')
          || (action === 'email' && !['admin','boss'].includes(vm.user?.role))) {finish('denied');return;}
        if (vm.modal?.type || vm.loading) {finish('busy');return;}
        const actorId = vm.user.id, generation = vm.authGeneration;
        try {
          // The cached complete workspace may be on another original menu.
          // Use its guarded navigation rather than writing activePage directly.
          if (vm.activePage !== 'orders') await vm.go('orders');
          if (!ready() || vm.user.id !== actorId || vm.authGeneration !== generation
            || vm.activePage !== 'orders' || !vm.canCreateOrders || !vm.hasPermission('orders.view')) {finish('denied');return;}
          if (vm.modal?.type || vm.loading) {finish('busy');return;}
          if (action === 'new') await vm.openOrder();
          else if (action === 'import') await vm.openOrderPdfImport();
          else await vm.openEmailQueue();
          if (!ready() || vm.user.id !== actorId || vm.authGeneration !== generation) {finish('denied');return;}
          finish(vm.modal?.type === (action === 'new' ? 'order' : 'orderPdfImport') ? 'opened' : 'failed');
        } catch { finish('failed'); }
      };
      const runCommand = async data => {
        const {key, requestId, actorId, generation} = data;
        if (typeof key !== 'string' || typeof requestId !== 'string' || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(requestId)) return;
        const reply = status => window.parent.postMessage({type:'tianming-unified-command-result-v1',requestId,key,status},location.origin);
        if (!connected || !unified || !activeSurface || !ready() || actorId !== vm.user.id || generation !== vm.authGeneration) {reply('denied');return;}
        const prior = commands.get(requestId);
        if (prior) {reply(prior.key === key ? (prior.status || 'busy') : 'denied');return;}
        if (commandBusy) {reply('busy');return;}
        const attempt = {key,status:null}; commands.set(requestId,attempt);
        if (commands.size > 100) commands.delete(commands.keys().next().value);
        commandBusy = true;
        try { attempt.status = await window.ERPUnifiedNavigation.execute(vm,key); }
        catch { attempt.status = 'failed'; }
        finally { commandBusy = false; reply(attempt.status); publish(); }
      };
      const receive = event => {
        if (event.source !== window.parent || event.origin !== location.origin) return;
        if (event.data?.type === 'tianming-formal-shell-v1') {
          connected = true; unified = !!window.ERPUnifiedNavigation && event.data.unifiedNavigation === true;
          activeSurface = event.data.active !== false; window.erpWorkspaceActive = activeSurface; lastNavigation = ''; publish();
          if (activeSurface) vm.queueViewportPageMeasure?.();
          if (event.data.retrySession === true && !vm.user && window.erpSessionUnavailable && !window.erpCheckingSession) {
            window.erpCheckingSession = true;
            Promise.resolve(vm.checkSession()).finally(() => {window.erpCheckingSession=false;publish();});
          }
        }
        if (event.data?.type === 'tianming-unified-command-v1') { void runCommand(event.data); return; }
        if (event.data?.type === 'tianming-formal-order-entry-v1') { void openEntry(event.data); return; }
        if (event.data?.type !== 'tianming-formal-menu-v1' || !ready() || !connected) return;
        if (unified && (vm.modal?.type || vm.productionEntry || vm.loading || !activeSurface)) return;
        const key = event.data.key;
        if (typeof key !== 'string' || !(vm.menus || []).some(item => item.key === key)) return;
        Promise.resolve(vm.goMenu(key)).then(publish);
      };
      window.addEventListener('message', receive);
      // A successful existing save only invalidates the shell's cached list.
      // No payload, response data or mutation command crosses this boundary.
      const responseHook = window.axios?.interceptors?.response;
      const interceptor = responseHook?.use(response => {
        const method = String(response.config?.method || '').toLowerCase();
        const path = String(response.config?.url || '').split('?')[0];
        if (connected && ready() && ['post','put','patch','delete'].includes(method)
          && /^\/api\/orders(?:\/(?:\d+|items\/\d+))?$/.test(path)
          && response.status >= 200 && response.status < 300) {
          window.parent.postMessage({type:'tianming-formal-orders-changed-v1',actorId:vm.user.id}, location.origin);
        }
        return response;
      });
      const stop = vm.$watch(() => JSON.stringify({
        menus:(vm.menus || []).map(item => ({key:item.key,label:item.label})),
        page:vm.activePage, user:!!vm.user, forced:!!vm.user?.must_change_password,modal:vm.modal?.type
      }), publish, {flush:'post'});
      let lastDraft = '';
      const publishDraft = () => {
        if (!connected || !ready()) return;
        const state = window.ERPFrontendReliability?.state?.();
        if (!state) return;
        const message = {type:'tianming-formal-draft-state-v1',actorId:vm.user.id,generation:vm.authGeneration,...state};
        const snapshot = JSON.stringify(message);
        if (snapshot === lastDraft) return;
        lastDraft = snapshot; window.parent.postMessage(message,location.origin);
      };
      const stopDraft = vm.$watch(() => JSON.stringify(window.ERPFrontendReliability?.state?.()),publishDraft,{flush:'post'});
      const timer = window.setInterval(publish, 1000);
      const visible = () => {if (!document.hidden && activeSurface) {lastNavigation='';publish();vm.refreshEmailQueueCount?.();}};
      document.addEventListener?.('visibilitychange',visible);
      window.addEventListener('pagehide', () => {stop();stopDraft();document.removeEventListener?.('visibilitychange',visible);clearInterval(timer);window.removeEventListener('message',receive);if(interceptor!==undefined)responseHook.eject(interceptor);}, {once:true});
      publish();
      // Do not wait for the iframe load event (images and other resources may
      // still be pending). The parent validates this exact frame and origin.
      window.parent.postMessage({type:'tianming-formal-bridge-ready-v1'}, location.origin);
    }
  };
  const style = document.createElement('style');
  style.textContent = '.frontend-shell-connected .app-shell > .layout > .sidebar{display:none}.frontend-shell-connected .app-shell > .layout{grid-template-columns:minmax(0,1fr)}';
  style.textContent += '.frontend-shell-unified .app-shell{grid-template-rows:minmax(0,1fr)}.frontend-shell-unified .app-shell>.topbar,.frontend-shell-unified .business-flow-guide,.frontend-shell-unified .workbench-nav,.frontend-shell-unified .warehouse-workspace-nav,.frontend-shell-unified .finance-task-nav{display:none!important}';
  style.textContent += '.frontend-shell-layout .app-shell>.layout>.sidebar{display:none}.frontend-shell-layout .app-shell>.layout{grid-template-columns:minmax(0,1fr)}.frontend-shell-layout .app-shell{grid-template-rows:minmax(0,1fr)}.frontend-shell-layout .app-shell>.topbar,.frontend-shell-layout .business-flow-guide,.frontend-shell-layout .workbench-nav,.frontend-shell-layout .warehouse-workspace-nav,.frontend-shell-layout .finance-task-nav{display:none!important}';
  document.head.appendChild(style);
})();
