let runtimePromise;
const cache=new Map();
export const normalizeSearch=value=>String(value||"").normalize("NFKC").toLowerCase().replace(/\s+/g,"");
export function goodsSearchText(value, runtime) {
  const source=String(value||"");
  const key=`${Boolean(runtime)}:${source}`;
  if(cache.has(key))return cache.get(key);
  let text=normalizeSearch(source);
  if(runtime?.pinyin){
    text += " " + normalizeSearch(runtime.pinyin(source,{toneType:"none"}));
    text += " " + normalizeSearch(runtime.pinyin(source,{pattern:"first",toneType:"none"}));
  }
  if(cache.size>10000)cache.clear();
  cache.set(key,text);return text;
}
export function matchesGoodsSearch(text,query) {
  return String(query||"").trim().split(/\s+/).every(term=>text.includes(normalizeSearch(term)));
}
export function loadGoodsPinyin() {
  if(window.pinyinPro?.pinyin)return Promise.resolve(window.pinyinPro);
  if(!runtimePromise)runtimePromise=new Promise(resolve=>{
    const script=document.createElement("script");
    script.src="/static/vendor/pinyin-pro-3.26.0.js";
    script.onload=()=>resolve(window.pinyinPro||null);
    script.onerror=()=>resolve(null);
    document.head.appendChild(script);
  });
  return runtimePromise;
}
