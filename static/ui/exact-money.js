(function(root){
 'use strict';
 const LIMIT=30;
 function parts(value){
  let text=String(value == null || value === '' ? '0' : value).trim();
  if(text.length>100)throw new Error('金额超出支持范围');
  const m=/^([+-]?)(\d*)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(text);
  if(!m || (!m[2] && !m[3]))throw new Error('无效金额');
  let digits=(m[2]||'0')+(m[3]||'');
  let scale=(m[3]||'').length-Number(m[4]||0);
  digits=digits.replace(/^0+/,'')||'0';
  if(digits==='0')return [0n,0];
  while(scale>0 && digits.endsWith('0')){digits=digits.slice(0,-1);scale--;}
  if(scale<0){if(-scale>20)throw new Error('金额超出支持范围');digits+='0'.repeat(-scale);scale=0;}
  if(scale>10 || digits.length>LIMIT || digits.length-scale>20)throw new Error('金额超出支持范围');
  return [BigInt((m[1]==='-'?'-':'')+digits),scale];
 }
 function render(n,scale){
  const negative=n<0n;if(negative)n=-n;
  let text=n.toString().padStart(scale+1,'0');
  if(scale)text=text.slice(0,-scale)+'.'+text.slice(-scale);
  if(text.includes('.'))text=text.replace(/0+$/,'').replace(/\.$/,'');
  return (negative && n!==0n?'-':'')+text;
 }
 function format(value){try{const [n,s]=parts(value);return render(n,s);}catch{return '—';}}
 function multiply(a,b){
  const [x,s]=parts(a),[y,t]=parts(b);
  const result=render(x*y,s+t);parts(result);return result;
 }
 function sum(values){
  let result=0n,scale=0;
  for(const v of values){const [n,s]=parts(v);const next=Math.max(scale,s);
   result=result*10n**BigInt(next-scale)+n*10n**BigInt(next-s);scale=next;}
  const value=render(result,scale);parts(value);return value;
 }
 const api=Object.freeze({format,multiply,sum});root.TmExactMoney=api;
 if(typeof module==='object' && module.exports)module.exports=api;
})(typeof globalThis==='object'?globalThis:this);
