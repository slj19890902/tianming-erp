(function (global) {
  "use strict";
  const prefix = "tm.mobile.field-return.v1:", lifetime = 8 * 60 * 60 * 1000, limit = 20;
  const positive = n => Number.isSafeInteger(n) && n > 0;
  const tokenValid = token => typeof token === "string" && /^[a-f0-9]{32}$/.test(token);
  const labelValid = value => typeof value === "string" && value.length <= 40 && !/[\x00-\x1f\x7f]/.test(value);
  const fieldTolerance = value => value === undefined ? "5" : value;
  const fieldMaterial = value => value === undefined ? "" : value;
  const fieldFlute = value => value === undefined ? "" : value;
  const fieldBoardState = value => value === undefined ? "raw" : value;
  function clean(value, owner, now) {
    if (!value || value.version !== 1 || value.actorId !== owner || !positive(owner) || !["search", "map", "field"].includes(value.kind) ||
        !Number.isSafeInteger(value.createdAt) || value.createdAt > now + 60000 || value.createdAt < now - lifetime ||
        typeof value.query !== "string" || value.query.length > 200 || typeof value.includeZero !== "boolean" ||
        (value.selectedProductId !== null && !positive(value.selectedProductId)) ||
        (value.locationId !== null && !positive(value.locationId)) || !labelValid(value.floorCode) || !labelValid(value.areaCode) ||
        !Number.isFinite(value.scrollY) || value.scrollY < 0 || value.scrollY > 1000000 ||
        (value.kind === "field" && (!["product", "board", "location"].includes(value.intent) ||
          !Number.isSafeInteger(value.page) || value.page < 1 || value.page > 10000 ||
          !["all", "orders", "materials", "molds", "production", "inventory"].includes(value.category) ||
          !Number.isSafeInteger(value.lookupPage) || value.lookupPage < 1 || value.lookupPage > 10000 ||
          (value.nearPage !== undefined && (!Number.isSafeInteger(value.nearPage) || value.nearPage < 1 || value.nearPage > 10000)) ||
          !["0", "5", "10"].includes(fieldTolerance(value.fieldTolerance)) ||
          typeof fieldMaterial(value.fieldMaterial) !== "string" || fieldMaterial(value.fieldMaterial).length > 60 || /[\x00-\x1f\x7f]/.test(fieldMaterial(value.fieldMaterial)) ||
          !["", "A", "B", "E", "AB", "BE", "ABC", "AAA", "NONE"].includes(fieldFlute(value.fieldFlute)) ||
          !["raw", "net_raw", "creased", "printed", "die_cut"].includes(fieldBoardState(value.fieldBoardState))))) return null;
    // Only navigation/input survives. Inventory responses and quantities are never cached.
    return {version:1,actorId:owner,createdAt:value.createdAt,kind:value.kind,query:value.query,includeZero:value.includeZero,
      selectedProductId:value.selectedProductId,locationId:value.locationId,floorCode:value.floorCode,areaCode:value.areaCode,scrollY:value.scrollY,
      ...(value.kind === "field" ? {intent:value.intent,page:value.page,category:value.category,lookupPage:value.lookupPage,nearPage:value.nearPage||1,
        fieldTolerance:fieldTolerance(value.fieldTolerance),fieldMaterial:fieldMaterial(value.fieldMaterial),fieldFlute:fieldFlute(value.fieldFlute),fieldBoardState:fieldBoardState(value.fieldBoardState)} : {})};
  }
  function randomToken() {
    const bytes = new Uint8Array(16);
    if (global.crypto?.getRandomValues) global.crypto.getRandomValues(bytes);
    else for (let i=0;i<bytes.length;i++) bytes[i] = Math.floor(Math.random()*256);
    return [...bytes].map(n=>n.toString(16).padStart(2,"0")).join("");
  }
  function capture(storage, owner, values, now = Date.now()) {
    const record = clean({version:1,actorId:owner,createdAt:now,...values},owner,now);
    if (!record) return null;
    try {
      const owned = [], keys = [];
      for (let i=0;i<storage.length;i++) {const key=storage.key(i);if(key?.startsWith(prefix))keys.push(key);}
      for (const key of keys) {
        let row=null;try{row=JSON.parse(storage.getItem(key));}catch(_error){}
        if (!row || !Number.isSafeInteger(row.createdAt) || row.createdAt < now-lifetime) storage.removeItem(key);
        else if (key.startsWith(prefix+owner+":")) owned.push({key,createdAt:row.createdAt});
      }
      owned.sort((a,b)=>b.createdAt-a.createdAt);
      for (const row of owned.slice(limit-1)) storage.removeItem(row.key);
      for(let attempt=0;attempt<8;attempt++) {
        const token=randomToken(),key=prefix+owner+":"+token;
        if(storage.getItem(key)!==null)continue;
        const raw=JSON.stringify(record);storage.setItem(key,raw);
        if(storage.getItem(key)!==raw)return null;
        return token;
      }
    } catch (_error) { /* A failed convenience cache must not block navigation or touch business records. */ }
    return null;
  }
  function read(storage, owner, token, now = Date.now()) {
    if(!positive(owner)||!tokenValid(token))return null;
    try{return clean(JSON.parse(storage.getItem(prefix+owner+":"+token)||"null"),owner,now);}catch(_error){return null;}
  }
  function scanTarget(id, product) {
    const raw=String(id||"");
    if(!/^[1-9]\d*$/.test(raw)||!positive(Number(raw))||(product && !/^[a-f0-9]{24}$/.test(product)))return null;
    return "/q/"+raw+(product?"/"+product:"");
  }
  function target(params) {
    if(params.get("return_scan")==="1") {
      const href=scanTarget(params.get("location_id"),params.get("return_product"));
      return href?{href,label:"返回扫码货位"}:null;
    }
    if(params.get("return_source")==="search") {
      const token=params.get("return_context"),query=new URLSearchParams({return_source:"search"});
      if(tokenValid(token))query.set("return_context",token);
      return {href:"/mobile/?"+query+"#warehouse",label:"返回查货"};
    }
    if(params.get("return_source")==="field") {
      const token=params.get("return_context"),query=new URLSearchParams({return_source:"field"});
      if(tokenValid(token))query.set("return_context",token);
      return {href:"/mobile/?"+query+"#lookup",label:"返回现场查询"};
    }
    return null;
  }
  global.TmMobileFieldReturn = {capture,read,target,scanTarget,prefix,lifetime,limit};
})(window);
