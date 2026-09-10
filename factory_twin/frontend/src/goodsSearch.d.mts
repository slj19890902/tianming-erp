export interface GoodsPinyin {pinyin:(text:string,options:Record<string,string>)=>string}
export function normalizeSearch(value:unknown):string;
export function goodsSearchText(value:unknown,runtime:GoodsPinyin|null):string;
export function matchesGoodsSearch(text:string,query:string):boolean;
export function loadGoodsPinyin():Promise<GoodsPinyin|null>;
