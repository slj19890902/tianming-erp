/* Load only a runtime compatible with this document; old cached pages reload first. */
(() => {
  const required=["inboundCandidates","goodsTypes","sheetGoods","finishedGoods","inboundNotListed","inboundEditProduct"];
  if(required.some(id=>!document.getElementById(id))||typeof returnToWarehouseContext!=="function"){
    const url=new URL(window.location.href);
    if(url.searchParams.get("stocktake_ui")!=="5"){
      url.searchParams.set("stocktake_ui","5");
      url.searchParams.set("refresh",String(Date.now()));
      window.location.replace(url.toString());
    }else{
      const box=document.getElementById("message")||document.body;
      const link=document.createElement("a");
      url.searchParams.set("refresh",String(Date.now()));
      link.href=url.toString();link.textContent="页面版本已更新，点击重新加载盘点页面";
      box.replaceChildren(link);
    }
    return;
  }
  if(window.mobileStocktakeRuntimeLoading)return;
  window.mobileStocktakeRuntimeLoading=true;
  const script=document.createElement("script");
  script.src="/mobile/initial-stocktake-runtime.js?ui=5";
  script.onerror=()=>{
    window.mobileStocktakeRuntimeLoading=false;
    const box=document.getElementById("message");
    if(box)box.textContent="盘点功能加载失败，请刷新重试";
  };
  document.head.appendChild(script);
})();
