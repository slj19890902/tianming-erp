(function(root) {
  'use strict';
  const integer=(value,label)=>{
    const result=Number(value);
    if(value==='' || typeof value==='boolean' || !Number.isSafeInteger(result) || result<1) throw new Error(label+'须为正整数');
    return result;
  };
  const hundredths=(value,label)=>{
    const text=String(value??'');
    if(!/^\d+(?:\.\d{1,2})?$/.test(text)) throw new Error(label+'须为正数，最多两位小数');
    const [whole,fraction='']=text.split('.');
    const result=Number(whole)*100+Number(fraction.padEnd(2,'0'));
    if(!Number.isSafeInteger(result) || result<=0 || result>999999999999) throw new Error(label+'超出可用尺寸');
    return result;
  };
  const decimal=value=>(Math.floor(value/100)+'.'+String(value%100).padStart(2,'0')).replace(/\.?0+$/,'');
  function contract(length,width,settings) {
    const a=integer(settings.length_parts,'长向份数'),b=integer(settings.width_parts,'宽向份数');
    const m=integer(settings.mold_count,'模数');
    if(typeof settings.is_die_cut!=='boolean' || (!settings.is_die_cut && m!==1)) throw new Error('非模切产品的模数必须为一');
    const l=hundredths(length,'理论报料长'),w=hundredths(width,'理论报料宽');
    const c=integer(a*b,'开料份数'),output=integer(c*m,'每张产出');
    const sl=hundredths(decimal(l*a),'供应商报料长'),sw=hundredths(decimal(w*b),'供应商报料宽');
    return {schema_version:2,theoretical_length_mm:decimal(l),theoretical_width_mm:decimal(w),
      length_parts:a,width_parts:b,is_die_cut:settings.is_die_cut,mold_count:m,
      supplier_length_mm:decimal(sl),supplier_width_mm:decimal(sw),cutting_factor:c,yield_per_supplier_sheet:output};
  }
  const label=n=>'一开'+({1:'一',2:'二',3:'三',4:'四',5:'五',6:'六'}[n]||n);
  function summary(snapshot) {
    if(!snapshot) return '';
    return `${label(snapshot.cutting_factor)}（长${snapshot.length_parts}×宽${snapshot.width_parts}）${snapshot.is_die_cut?' · '+snapshot.mold_count+'模':''} · 每张出${snapshot.yield_per_supplier_sheet}片`;
  }
  const api={contract,label,summary};
  if(typeof module!=='undefined' && module.exports) module.exports=api;
  else root.ERPSheetCutting=api;
})(typeof globalThis!=='undefined'?globalThis:this);
