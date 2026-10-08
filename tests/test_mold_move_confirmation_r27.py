from pathlib import Path
import json,subprocess,shutil,re
ROOT=Path(__file__).resolve().parents[1]
HTML=(ROOT/'static/warehouse.html').read_text('utf-8')
def extract(name,nextname):
 start=HTML.index('    '+name);end=HTML.index('    '+nextname,start+1);return HTML[start:end]
def test_mold_move_confirmation_uses_preview_readable_names_and_keeps_raw_payload():
 helpers=''
 if 'function moldMoveConfirmationLocation(' in HTML:
  helpers=extract('function moldMoveConfirmationLocation(', 'function showPageError(')
 save=extract('async function saveMold(', 'async function enableLegacyMold(')
 script=r'''
 const vm=require('vm');
 (async()=>{
 for(const accept of [false,true]) for(const missing of [false,true]){
  const elements={moldId:{value:'1'},moldLabelName:{value:'虚构模具'},moldRackLocation:{value:'MCELL-TARGET'},moldChineseShortName:{value:''},moldRemarks:{value:''},moldVersion:{value:'4'},moldCode:{value:'M-FAKE'},moldSaveButton:{disabled:false}};
  const calls=[],messages=[],confirms=[];
  const state={moldSaving:false,moldLabelPreview:{loaded:false},moldMasterAttempt:null,moldEditLocation:{originalLocation:'MCELL-SOURCE',attempt:null}};
  const preview={same_location:false,expected_version:7,target_location:'MCELL-TARGET',co_located_count:2,mold:{rack_location:'MCELL-SOURCE',location_guide:missing?{}:{prompt:'三楼 · 虚构模具架 · 源格'}},target_guide:missing?{}:{prompt:'三楼 · 虚构模具架 · 目标格'}};
  let counter=0;
  const ctx={state,$:id=>elements[id],toast:m=>messages.push(m),moldCustomerAssociations:()=>[{customer_id:1,is_primary:true}],createIdempotencyKey:()=>`fictional-key-${++counter}`,window:{confirm:m=>{confirms.push(m);return accept}},api:async(path,init)=>{calls.push({path,method:init?.method,body:init?.body?JSON.parse(init.body):null});return path.endsWith('/preview')?preview:{id:1}},loadMolds:async()=>true,finishMoldSave:()=>{},updateMoldSaveAvailability:()=>{}};
  vm.createContext(ctx);vm.runInContext(HELPERS+SAVE,ctx);await ctx.saveMold({preventDefault(){}});
  if(confirms.length!==1)throw new Error('expected one physical-move confirmation');
  if(/MCELL-|M-FAKE/.test(confirms[0]))throw new Error('internal identity leaked into confirmation: '+confirms[0]);
  if(missing){if(!confirms[0].includes('位置名称待完善'))throw new Error('missing readable guide must not fall back to raw identifiers');}
  else{if(!confirms[0].includes('原位置：三楼 · 虚构模具架 · 源格')||!confirms[0].includes('新位置：三楼 · 虚构模具架 · 目标格'))throw new Error('readable current/target preview identities not shown');}
  if(!confirms[0].includes('2 件模具'))throw new Error('co-located warning lost');
  const writes=calls.filter(c=>c.method==='PUT');
  if(!accept){if(writes.length||state.moldMasterAttempt||state.moldEditLocation.attempt)throw new Error('cancel performed a save or froze an attempt');}
  else{if(writes.length!==1)throw new Error('physical move must save once');let p=writes[0].body;if(p.rack_location!=='MCELL-TARGET'||p.expected_version!==4||p.expected_location_version!==7||!p.physical_move_confirmed||!p.location_idempotency_key||!p.idempotency_key)throw new Error('identity, version, confirmation or idempotency payload gate changed');}
 }
 })().catch(e=>{console.error(e.message);process.exitCode=1});
 '''.replace('HELPERS',json.dumps(helpers)).replace('SAVE',json.dumps(save))
 result=subprocess.run([shutil.which('node') or 'C:/Program Files/nodejs/node.exe'],input=script,text=True,encoding='utf-8',capture_output=True)
 assert result.returncode==0,result.stderr

def test_timeline_uses_readable_names_and_never_falls_back_to_internal_cell_ids():
 helpers=''
 if 'function timelineLocationName(' in HTML:
  helpers=extract('function timelineLocationName(', 'function timelineEventDetail(')
 body=extract('function timelineEventDetail(', 'function timelineHtml(')
 script=r'''
 const vm=require('vm');const ctx={};vm.createContext(ctx);vm.runInContext(HELPERS+BODY,ctx);
 for(const event of [
  {from_location:'MCELL-SOURCE',to_location:'MCELL-TARGET',from_location_name:'三楼 源格',to_location_name:'三楼 目标格'},
  {from_location:'MCELL-SOURCE',to_location:'MCELL-TARGET'},
  {from_location:'3F-M-R01-L1-G01',to_location:'1F-M-R04-L1-V-P01'},
  {from_location:'二楼旧模具架',to_location:'三楼旧模具架'},
 ]){
  event.operator_name='虚构操作员';event.note='原移位记录';const text=ctx.timelineEventDetail(event);
  if(/MCELL-|[134]F-M-R/.test(text))throw new Error('internal position code leaked into timeline: '+text);
  if(event.from_location_name && !text.includes('三楼 源格 → 三楼 目标格'))throw new Error('readable movement names lost');
  if(event.from_location==='二楼旧模具架' && !text.includes('二楼旧模具架 → 三楼旧模具架'))throw new Error('legacy readable names changed');
  if(!text.includes('原移位记录')||!text.includes('虚构操作员'))throw new Error('original trace detail lost');
 }
 '''.replace('HELPERS',json.dumps(helpers)).replace('BODY',json.dumps(body))
 r=subprocess.run([shutil.which('node') or 'C:/Program Files/nodejs/node.exe'],input=script,text=True,encoding='utf-8',capture_output=True)
 assert r.returncode==0,r.stderr

def test_mold_timeline_adds_names_without_changing_stored_movement_or_writing(tmp_path):
 from app.core.database import create_sqlite_engine
 from app.models import Base
 from app.models.mold_tool import MoldTool,MoldLocationMovement
 from app.services.asset_time_archive import build_mold_detail_timeline
 from sqlalchemy.orm import Session
 engine=create_sqlite_engine(tmp_path/'fictional-mold-timeline.sqlite3');Base.metadata.create_all(engine)
 with Session(engine) as db:
  mold=MoldTool(mold_code='R27-FICTIONAL',mold_name='虚构回归模具',rack_location='3F-M-R02-L2-G02',location_version=2)
  db.add(mold);db.flush()
  movement=MoldLocationMovement(mold_tool_id=mold.id,mold_code_snapshot=mold.mold_code,from_location='3F-M-R01-L1-G01',to_location=mold.rack_location,expected_version=1,resulting_version=2,idempotency_key='r27-fictional-move',source='manual_input')
  db.add(movement);db.commit();before=db.connection().exec_driver_sql('SELECT total_changes()').scalar()
  result=build_mold_detail_timeline(db,mold,products=[],allowed_customer_ids=None)
  row=next(x for x in result['timeline'] if x['event_type']=='mold_location_move')
  assert row['from_location']==movement.from_location and row['to_location']==movement.to_location
  assert '三楼' in row['from_location_name'] and '第1层' in row['from_location_name']
  assert '三楼' in row['to_location_name'] and '第2层' in row['to_location_name']
  assert row['expected_version']==1 and row['resulting_version']==2
  assert db.connection().exec_driver_sql('SELECT total_changes()').scalar()==before
  assert not db.dirty and not db.new and not db.deleted
