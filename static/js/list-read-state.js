(function(global) {
  'use strict';
  const flights = new WeakMap();
  const cancelled = error => ['ERR_CANCELED','CanceledError','AbortError'].includes(error?.code || error?.name);
  function begin(root, options) {
    const {key, scope=()=>'', context=()=>root.activePage, clear=()=>{}} = options;
    const identity = () => `${root.user?.id ?? 'anonymous'}:${root.authGeneration}:${scope()}`;
    const owner = identity(), startedContext = context(), pageSequence = root.pageLoadSequence;
    root.listReadStates ||= {};
    root.listReadStates[key] ||= {identity:'',loaded:false,loading:false,error:''};
    const state = root.listReadStates[key];
    const publish = () => {
      if (options.loadingField) root[options.loadingField] = state.loading;
      if (options.errorField) root[options.errorField] = state.error;
    };
    if (state.identity !== owner) { clear(); Object.assign(state,{identity:owner,loaded:false,error:''}); }
    const controller = root.beginLatestRequest(key), token = {};
    let active = flights.get(root);
    if (!active) {active = new Map(); flights.set(root,active);}
    active.set(key,token); state.loading=true;state.error='';publish();
    const owns = () => active.get(key) === token && root.listReadStates[key] === state && state.identity === owner;
    const current = () => owns() && identity() === owner && !controller.signal.aborted && context() === startedContext && root.pageLoadSequence === pageSequence;
    return {
      signal:controller.signal, current,
      accept(apply) {if (!current()) return false;apply();state.loaded=true;return true;},
      fail(error) {if (current() && !cancelled(error)) {state.error=root.errorMessage(error) || '读取失败，请重试';publish();}return false;},
      finish() {if (owns()) {state.loading=false;publish();}if(active.get(key)===token)active.delete(key);root.finishLatestRequest(key,controller);},
    };
  }
  const component = {
    props:{state:Object,hasRows:Boolean,loadingText:{type:String,default:'正在读取列表…'},emptyText:{type:String,default:'当前没有符合条件的数据'}},
    emits:['retry'],
    template:`<div class="list-read-state" :aria-busy="!!state?.loading">
      <div v-if="state?.loading" class="product-list-status" role="status">{{hasRows?'正在更新，以下保留上次结果…':loadingText}}</div>
      <div v-if="state?.error" class="notice error" role="alert">{{hasRows?'更新失败，以下保留上次结果：':'读取失败：'}}{{state.error}} <button class="btn small" :disabled="state.loading" @click="$emit('retry')">重试</button></div>
      <div v-if="state?.loaded && !state.loading && !state.error && !hasRows" class="panel empty">{{emptyText}}</div>
    </div>`,
  };
  global.ERPListRead = {begin,component,install(app){app.component('list-read-status',component);}};
})(window);
