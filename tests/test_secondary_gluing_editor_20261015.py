import json
from tests.test_p1_09c_104_finished_reservation_action_guard import _method_contents,_run_node


def test_secondary_gluing_is_editable_and_not_reintroduced_from_legacy(tmp_path):
    methods={n:_method_contents(n) for n in ('parseProductionProcesses','unmanagedProductionProcesses','serializeProductionProcesses')}
    script='const methods='+json.dumps(methods)+';const fn=Object.fromEntries(Object.entries(methods).map(([n,b])=>[n,new Function(...(n==="serializeProductionProcesses"?["values","legacyValue"]:["value"]),b)]));'+'''
    const original='模切、粘合、二次粘合、特殊生产标记';
    const selected=fn.parseProductionProcesses(original);if(!selected.includes('二次粘合'))throw Error('secondary checkbox not hydrated');
    const legacy=fn.unmanagedProductionProcesses(original).join(',');if(legacy.includes('二次粘合')||!legacy.includes('特殊生产标记'))throw Error('managed process hidden in legacy');
    const cancelled=fn.serializeProductionProcesses(selected.filter(v=>v!=='二次粘合'),legacy+',二次粘合');
    if(cancelled.includes('二次粘合')||!cancelled.includes('特殊生产标记'))throw Error('cancelled secondary restored or unknown legacy lost');
    if(!fn.serializeProductionProcesses(selected,legacy).includes('二次粘合'))throw Error('selected secondary lost');
    '''
    _run_node(script,tmp_path,'secondary-roundtrip.js')


def test_secondary_gluing_dependencies_need_no_extra_confirmation(tmp_path):
    body=_method_contents('onProductProcessesChanged')
    script='const fn=new Function("event",'+json.dumps(body)+');'+'''
    const vm={productForm:{box_style:'A1箱',box_category:'normal',_production_processes:['打钉','二次粘合']},syncProductSheetCuttingSettings(){},productUsesMold(){return this.productForm._production_processes.includes('模切')}};
    globalThis.confirm=()=>{throw Error('unexpected confirmation')};
    fn.call(vm,{target:{value:'二次粘合',checked:true}});
    const f=vm.productForm;if(f.box_style!=='模切内盒'||f.box_category!=='die_cut'||!['粘贴','模切','二次粘合'].every(v=>f._production_processes.includes(v))||f._production_processes.includes('打钉'))throw Error('invalid secondary dependency');
    f._production_processes=['粘贴','二次粘合'];fn.call(vm,{target:{value:'模切',checked:false}});if(f._production_processes.includes('二次粘合'))throw Error('removed cutting kept secondary');
    '''
    _run_node(script,tmp_path,'secondary-dependencies.js')
