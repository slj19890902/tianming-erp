(function(global){
  'use strict';
  const BASE='/api/home-tools';
  const key=()=>global.crypto.randomUUID ? global.crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,c=>{const n=global.crypto.getRandomValues(new Uint8Array(1))[0]&15;return(c==='x'?n:(n&3)|8).toString(16);});
  const message=e=>typeof e?.response?.data?.detail==='string'?e.response.data.detail:e?.code==='ECONNABORTED'?'连接超时，输入已保留，可重试原操作':'暂时无法保存或读取，输入已保留，请重试';
  const size=n=>n>=1048576?(n/1048576).toFixed(1)+' MB':Math.max(1,Math.ceil(n/1024))+' KB';
  const time=t=>t?new Date(t).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'';
  const component={
    components:global.ElementPlusIconsVue||{},
    props:{actorId:{type:Number,required:true}},
    data(){return {memos:[],files:[],memoTotal:0,fileTotal:0,activeCount:0,doneCount:0,
      draft:'',newId:key(),attempts:{},editId:'',editText:'',editVersion:0,
      memoAll:false,filesAll:false,completed:false,trash:false,memoPage:1,filePage:1,query:'',
      memoBusy:false,fileBusy:false,memoLoading:false,fileLoading:false,memoError:'',fileError:'',memoConflict:false,
      uploadTarget:null,pendingUpload:null,historyId:'',history:[],historyTotal:0,historyPage:1,
      scope:0,memoSeq:0,fileSeq:0,notice:''};},
    computed:{memoLimit(){return this.memoAll?20:4;},fileLimit(){return this.filesAll?20:4;}},
    watch:{actorId:{immediate:true,handler(){
      this.scope++;this.memoSeq++;this.fileSeq++;this.memos=[];this.files=[];this.draft='';this.editId='';this.editText='';
      this.attempts={};this.pendingUpload=null;this.uploadTarget=null;this.historyId='';this.history=[];
      this.memoError='';this.fileError='';this.notice='';this.memoBusy=false;this.fileBusy=false;
      this.memoPage=1;this.filePage=1;this.memoTotal=0;this.fileTotal=0;this.activeCount=0;this.doneCount=0;
      if(this.actorId){this.loadMemos();this.loadFiles();}
    }}},
    mounted(){this._unload=e=>{if(this.draft.trim()||this.editId||this.pendingUpload||this.memoBusy||this.fileBusy){e.preventDefault();e.returnValue='';}};global.addEventListener('beforeunload',this._unload);},
    beforeUnmount(){this.scope++;global.removeEventListener('beforeunload',this._unload);},
    methods:{size,time,
      http(){return global.axios.create({withCredentials:true,timeout:30000});},
      attempt(name,body){const fingerprint=JSON.stringify(body);if(this.attempts[name]?.fingerprint!==fingerprint)this.attempts[name]={fingerprint,key:key()};return {...body,request_key:this.attempts[name].key};},
      async loadMemos(){if(!this.actorId)return;const s=this.scope,n=++this.memoSeq;this.memoLoading=true;
        try{const {data}=await this.http().get(BASE+'/memos',{params:{completed:this.completed,page:this.memoPage,limit:this.memoLimit}});
          if(s!==this.scope||n!==this.memoSeq)return;this.memos=data.items;this.memoTotal=data.total;this.activeCount=data.active_count;this.doneCount=data.completed_count;
          if(this.memoPage>1&&!data.items.length){this.memoPage--;return this.loadMemos();}
        }catch(e){if(s===this.scope)this.memoError=message(e);}finally{if(s===this.scope&&n===this.memoSeq)this.memoLoading=false;}},
      async loadFiles(){if(!this.actorId)return;const s=this.scope,n=++this.fileSeq;this.fileLoading=true;
        try{const {data}=await this.http().get(BASE+'/files',{params:{q:this.query,deleted:this.trash,page:this.filePage,limit:this.fileLimit}});
          if(s!==this.scope||n!==this.fileSeq)return;this.files=data.items;this.fileTotal=data.total;
          if(this.filePage>1&&!data.items.length){this.filePage--;return this.loadFiles();}
        }catch(e){if(s===this.scope)this.fileError=message(e);}finally{if(s===this.scope&&n===this.fileSeq)this.fileLoading=false;}},
      async saveMemo(row,changes={}){
        if(this.memoBusy||!this.actorId)return;
        const body={id:row?.id||this.newId,text:row?.text||this.draft.trim(),completed:row?.completed||false,deleted:false,
          expected_version:row?.version||0,...changes};if(!body.text.trim())return;
        const s=this.scope,request=this.attempt('memo:'+body.id,body);this.memoBusy=true;this.memoError='';this.memoConflict=false;
        try{await this.http().post(BASE+'/memos',request);if(s!==this.scope)return;
          delete this.attempts['memo:'+body.id];if(!row){this.draft='';this.newId=key();this.completed=false;this.memoPage=1;}
          if(this.editId===body.id){this.editId='';this.editText='';}await this.loadMemos();
        }catch(e){if(s===this.scope){this.memoError=message(e);this.memoConflict=e?.response?.status===409;}}finally{if(s===this.scope)this.memoBusy=false;}
      },
      startEdit(row){if(this.memoBusy||this.editId)return;this.editId=row.id;this.editText=row.text;this.editVersion=row.version;},
      saveEdit(row){if(this.editText.trim()===row.text){this.editId='';return;}return this.saveMemo({...row,version:this.editVersion},{text:this.editText.trim()});},
      removeMemo(row){if(global.confirm('删除这条待办？'))return this.saveMemo(row,{deleted:true});},
      async retryEdit(){if(!this.editId||this.memoBusy)return;await this.loadMemos();const row=this.memos.find(r=>r.id===this.editId);if(!row){this.memoError='原待办已不在当前列表，修改内容仍保留；请先复制后取消编辑';return;}
        if(global.confirm('当前已保存：'+row.text+'\n本次修改：'+this.editText+'\n核对后，是否保存本次修改？')){this.editVersion=row.version;return this.saveEdit(row);}},
      showMemos(){if(this.editId||this.memoBusy)return;this.memoAll=!this.memoAll;this.memoPage=1;this.loadMemos();},
      showCompleted(){if(this.editId||this.memoBusy)return;this.completed=!this.completed;this.memoPage=1;this.loadMemos();},
      showFiles(){this.filesAll=!this.filesAll;this.filePage=1;if(!this.filesAll){this.trash=false;this.query='';this.historyId='';}this.loadFiles();},
      searchFiles(){this.filePage=1;this.fileError='';this.loadFiles();},
      chooseFile(row=null){if(this.fileBusy)return;this.uploadTarget=row?{id:row.id,version:row.version,name:row.name}:null;this.$refs.fileInput.value='';this.$refs.fileInput.click();},
      selectedFile(e){const f=e.target.files?.[0];if(f)this.prepareUpload(f,this.uploadTarget);e.target.value='';},
      dropped(e){if(this.fileBusy)return;const files=e.dataTransfer?.files;if(files?.length!==1){this.fileError='请每次上传一个文件';return;}this.prepareUpload(files[0],null);},
      prepareUpload(file,target){if(file.size>20*1048576){this.fileError='单文件不能超过20MB';return;}
        this.pendingUpload={file,target,key:key()};this.uploadFile();},
      async uploadFile(){if(!this.pendingUpload||this.fileBusy||!this.actorId)return;
        const s=this.scope,p=this.pendingUpload,form=new FormData();form.append('file',p.file);form.append('request_key',p.key);
        form.append('expected_version',p.target?.version||0);if(p.target)form.append('file_id',p.target.id);
        this.fileBusy=true;this.fileError='';this.notice='';
        try{await this.http().post(BASE+'/files',form,{timeout:120000});if(s!==this.scope)return;
          this.pendingUpload=null;this.uploadTarget=null;this.notice=p.target?'新版本已保存，旧版仍在历史版本中':'文件已上传，全员可见';
          this.filePage=1;this.trash=false;this.query='';this.historyId='';await this.loadFiles();
        }catch(e){if(s===this.scope)this.fileError=message(e);}finally{if(s===this.scope)this.fileBusy=false;}},
      async changeFileState(row,deleted){if(this.fileBusy||!row.can_manage)return;
        if(deleted&&!global.confirm('将“'+row.name+'”移入回收站？原件和历史版本会保留。'))return;
        const s=this.scope;this.fileBusy=true;this.fileError='';
        try{await this.http().post(BASE+'/files/'+row.id+'/state',this.attempt('state:'+row.id,{expected_version:row.version,deleted}));
          if(s!==this.scope)return;delete this.attempts['state:'+row.id];this.notice=deleted?'已移入回收站':'文件已恢复';this.historyId='';await this.loadFiles();
        }catch(e){if(s===this.scope)this.fileError=message(e);}finally{if(s===this.scope)this.fileBusy=false;}},
      async versions(row,page=1){if(this.historyId===row.id&&page===0){this.historyId='';return;}const s=this.scope;
        this.historyId=row.id;this.history=[];this.historyPage=page;
        try{const {data}=await this.http().get(BASE+'/files/'+row.id+'/versions',{params:{page}});
          if(s===this.scope&&this.historyId===row.id&&this.historyPage===page){this.history=data.items;this.historyTotal=data.total;}
        }catch(e){if(s===this.scope)this.fileError=message(e);}},
      url(row,version,preview=false){return BASE+'/files/'+encodeURIComponent(row.id)+'/content?'+new URLSearchParams({version:version||row.content_version,preview:String(preview)});},
      canPreview(row){return row.content_type==='application/pdf'||row.content_type?.startsWith('image/');},
      icon(row){return row.name.split('.').pop().toUpperCase().replace('XLSX','XLS').replace('DOCX','DOC');},
    },
    template:`<div v-if="actorId" class="hc-grid" aria-label="待办和共享文件">
      <section class="hc-panel" aria-label="我的待办">
        <header><h2>我的待办 <small>仅自己可见</small></h2><button type="button" :disabled="memoBusy||!!editId" @click="showMemos">{{memoAll?'收起':'查看全部'}}</button></header>
        <form class="hc-add" @submit.prevent="saveMemo(null)"><input v-model="draft" maxlength="600" :disabled="memoBusy" aria-label="新待办" placeholder="写一条待办，回车添加" @keydown.enter="$event.isComposing && $event.preventDefault()"><button :disabled="memoBusy||!draft.trim()" aria-label="添加待办"><Plus class="hc-icon" aria-hidden="true"/></button></form>
        <p v-if="memoError" class="hc-error" role="alert">{{memoError}} <button v-if="memoConflict&&editId" type="button" @click="retryEdit">核对后重试</button><button type="button" @click="memoError='';loadMemos()">重新加载</button></p>
        <div class="hc-memos" :aria-busy="memoLoading"><p v-if="!memos.length" class="hc-empty">{{memoLoading?'读取中…':completed?'还没有已完成事项':'暂无待办，随手记一条'}}</p>
          <div v-for="row in memos" :key="row.id" class="hc-memo" :class="{done:row.completed}">
            <input type="checkbox" :checked="row.completed" :disabled="memoBusy||!!editId" :aria-label="(row.completed?'恢复待办：':'完成待办：')+row.text" @change="saveMemo(row,{completed:!row.completed})">
            <form v-if="editId===row.id" class="hc-edit" @submit.prevent="saveEdit(row)"><input v-model="editText" maxlength="600" aria-label="修改待办" :disabled="memoBusy" @blur="saveEdit(row)" @keydown.enter="$event.isComposing && $event.preventDefault()" @keydown.esc.prevent="editId=''"><button type="submit" :disabled="memoBusy">保存</button><button type="button" :disabled="memoBusy" @mousedown.prevent @click="editId=''">取消</button></form>
            <button v-else type="button" class="hc-memo-text" :title="row.text" @click="startEdit(row)">{{row.text}}</button>
            <button type="button" class="hc-remove" :disabled="memoBusy||!!editId" aria-label="删除待办" @click="removeMemo(row)"><Close class="hc-icon" aria-hidden="true"/></button>
          </div>
        </div>
        <footer><button type="button" :disabled="memoBusy||!!editId" @click="showCompleted">{{completed?'‹ 未完成 '+activeCount+' 项':'已完成 '+doneCount+' 项 ›'}}</button><span v-if="memoTotal>memoLimit" class="hc-pages"><button :disabled="memoPage<=1||memoLoading||memoBusy||!!editId" @click="memoPage--;loadMemos()">上一页</button>{{memoPage}} / {{Math.ceil(memoTotal/memoLimit)}}<button :disabled="memoPage*memoLimit>=memoTotal||memoLoading||memoBusy||!!editId" @click="memoPage++;loadMemos()">下一页</button></span></footer>
      </section>
      <section class="hc-panel" aria-label="共享文件" @dragover.prevent @drop.prevent="dropped">
        <header><h2>共享文件 <small>全员可见</small></h2><div><button type="button" @click="showFiles">{{filesAll?'收起':'全部文件'}}</button><button type="button" class="hc-primary" :disabled="fileBusy" @click="chooseFile()"><Upload class="hc-icon" aria-hidden="true"/> {{fileBusy?'保存中…':'上传文件'}}</button></div></header>
        <input ref="fileInput" type="file" hidden accept=".xlsx,.xls,.docx,.doc,.pdf,.png,.jpg,.jpeg,.webp" @change="selectedFile" @cancel="uploadTarget=null">
        <form v-if="filesAll" class="hc-search" @submit.prevent="searchFiles"><input v-model="query" maxlength="100" placeholder="搜索文件名" aria-label="搜索共享文件"><button>搜索</button><button type="button" :class="{selected:trash}" @click="trash=!trash;searchFiles()">{{trash?'返回文件':'回收站'}}</button></form>
        <p v-if="fileError" class="hc-error" role="alert">{{fileError}} <button v-if="pendingUpload" :disabled="fileBusy" @click="uploadFile">重试原上传</button><button @click="fileError='';loadFiles()">重新加载</button></p>
        <p v-if="notice" class="hc-notice" role="status">{{notice}}</p>
        <div class="hc-files" :aria-busy="fileLoading"><p v-if="!files.length" class="hc-empty">{{fileLoading?'读取中…':trash?'回收站为空':'暂无文件，可拖入或点击上传'}}</p>
          <article v-for="row in files" :key="row.id"><div class="hc-file-row"><span class="hc-file-icon" :class="icon(row).toLowerCase()"><Document class="hc-icon" aria-hidden="true"/></span>
            <div class="hc-file-name"><a :href="url(row,null,canPreview(row))" target="_blank" rel="noopener" :title="row.name">{{row.name}}</a><small>{{row.updated_by}} · {{time(row.updated_at)}} · {{size(row.size)}}</small></div>
            <a :href="url(row)" target="_blank" rel="noopener">下载</a>
            <details class="hc-menu"><summary :aria-label="row.name+'的更多操作'"><MoreFilled class="hc-icon" aria-hidden="true"/></summary><div><button v-if="!row.deleted" :disabled="fileBusy" @click="chooseFile(row)">更新版本</button><button @click="versions(row,historyId===row.id?0:1)">历史版本</button><button v-if="row.can_manage" :disabled="fileBusy" @click="changeFileState(row,!row.deleted)">{{row.deleted?'恢复文件':'移入回收站'}}</button></div></details>
          </div><div v-if="historyId===row.id" class="hc-history"><div v-for="v in history" :key="v.version"><span>第{{v.version}}版 · {{v.updated_by}} · {{time(v.created_at)}}</span><a :href="url(row,v.version)" target="_blank" rel="noopener">下载</a></div><nav v-if="historyTotal>20"><button :disabled="historyPage<=1" @click="versions(row,historyPage-1)">上一页</button>{{historyPage}}<button :disabled="historyPage*20>=historyTotal" @click="versions(row,historyPage+1)">下一页</button></nav></div></article>
        </div>
        <footer><span class="hc-hint">支持 Excel、Word、PDF、图片 · 单个 ≤20MB</span><span v-if="fileTotal>fileLimit" class="hc-pages"><button :disabled="filePage<=1||fileLoading" @click="filePage--;loadFiles()">上一页</button>{{filePage}} / {{Math.ceil(fileTotal/fileLimit)}}<button :disabled="filePage*fileLimit>=fileTotal||fileLoading" @click="filePage++;loadFiles()">下一页</button></span></footer>
        <p v-if="filesAll" class="hc-hint">下载后用 WPS／Office 修改，再从原文件的“更新版本”上传。本机保存不会自动同步。</p>
      </section>
    </div>`
  };
  const exported={component,key,message,size,time,install(app){app.component('home-collaboration',component);}};
  global.ERPHomeCollaboration=exported;if(typeof module!=='undefined'&&module.exports)module.exports=exported;
})(typeof window==='undefined'?globalThis:window);
