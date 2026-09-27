// Camera frames stay on-device; only the extracted location is sent to our existing API.
function shelfIdentity(text) {
  try {
    const url = new URL(text, location.origin);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
    const hosts = new Set([location.hostname, 'tianmingerp0909.share.zrok.io', '192.168.3.80', '172.16.1.26']);
    if (!hosts.has(url.hostname)) return null;
    const match = url.pathname.match(/^\/q\/([1-9]\d*)(?:\/([a-f0-9]{24}))?\/?$/);
    if (match) return {id:match[1], product:match[2] || null};
    if (['/warehouse.html', '/static/shelf-scan.html'].includes(url.pathname)) {
      const target = url.searchParams.get('location_id'), key = url.searchParams.get('product');
      if (/^[1-9]\d*$/.test(target || '') && (!key || /^[a-f0-9]{24}$/.test(key))) return {id:target, product:key};
    }
  } catch (_) { /* A QR may contain text rather than an ERP URL. */ }
  return null;
}
function moldIdentity(text) {
  try {
    const url = new URL(text, location.origin);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
    if (![location.hostname, 'tianmingerp0909.share.zrok.io', '192.168.3.80', '172.16.1.26'].includes(url.hostname)) return null;
    const match = url.pathname.match(/^\/M\/([1-9]\d*)\/?$/);
    if (match) {
      const task = url.searchParams.get('production_task_id');
      if (task && !/^[1-9]\d*$/.test(task)) return null;
      return `/M/${match[1]}${task ? '?production_task_id='+task : ''}`;
    }
    if (['/mobile/mold-lookup', '/static/mobile_mold_lookup.html'].includes(url.pathname)) {
      const moldId = url.searchParams.get('mold_id');
      if (/^[1-9]\d*$/.test(moldId || '')) return `/mobile/mold-lookup?mold_id=${moldId}&readonly=1`;
    }
  } catch (_) { /* Only recognized ERP identities are accepted. */ }
  return null;
}
function erpMobileRoute(text) {
  try {
    const url = new URL(text, location.origin);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
    const hosts = new Set([location.hostname, 'tianmingerp0909.share.zrok.io', '192.168.3.80', '172.16.1.26']);
    if (!hosts.has(url.hostname)) return null;
    let match = url.pathname.match(/^\/P\/([1-9]\d*)\/?$/);
    if (match) return `/P/${match[1]}`;
    match = url.pathname.match(/^\/I\/([1-9]\d*)\/?$/);
    if (match) return `/I/${match[1]}`;
    match = url.pathname.match(/^\/sp\/([1-9]\d*)\/([1-9]\d*)\/([1-9]\d*)\/([1-9]\d*)\/?$/);
    if (match) return match[0];
    if (url.pathname === '/static/finished-goods-label.html') {
      const lotId = url.searchParams.get('lot_id');
      if (/^[1-9]\d*$/.test(lotId || '')) return `/I/${lotId}`;
    }
    if (url.pathname === '/scan/rack' || url.pathname === '/warehouse.html') {
      const floor = (url.searchParams.get('floor') || '').toUpperCase();
      const rackId = url.searchParams.get('rack_id') || '';
      if (/^\d{1,2}F$/.test(floor) && /^[A-Za-z0-9._:-]{1,80}$/.test(rackId)) {
        return `/scan/rack?floor=${encodeURIComponent(floor)}&rack_id=${encodeURIComponent(rackId)}`;
      }
    }
    if (url.pathname === '/incoming.html') return '/mobile/?mobile_page=incoming#incoming';
    if (['/mobile/', '/mobile/erp.html'].includes(url.pathname)) {
      const page = url.searchParams.get('mobile_page') || (url.searchParams.get('task_id') ? 'production' : 'home');
      if (!['home','lookup','incoming','warehouse','production','pre_delivery'].includes(page)) return null;
      const output = new URLSearchParams({mobile_page: page});
      for (const [name, pattern] of [
        ['task_id', /^[1-9]\d*$/], ['location_id', /^[1-9]\d*$/], ['lot_id', /^[1-9]\d*$/],
        ['floor_code', /^\d{1,2}F$/], ['rack_id', /^[A-Za-z0-9._:-]{1,80}$/], ['warehouse_map', /^1$/]
      ]) {
        const value = url.searchParams.get(name);
        if (value && pattern.test(value)) output.set(name, value);
      }
      return `/mobile/?${output.toString()}#${page}`;
    }
    if (url.pathname === '/mobile/delivery-pick.html') {
      const taskId = url.searchParams.get('task_id');
      return taskId && /^[1-9]\d*$/.test(taskId) ? `/mobile/delivery-pick.html?task_id=${taskId}` : '/mobile/delivery-pick.html';
    }
    if (url.pathname === '/mobile/tianhua-pick') {
      const token = url.searchParams.get('token') || '';
      if (/^[A-Za-z0-9._~-]{16,2048}$/.test(token)) return `/mobile/tianhua-pick?token=${encodeURIComponent(token)}`;
    }
  } catch (_) { /* Only recognized ERP identities are accepted. */ }
  return null;
}
let cameraStream = null, cameraEpoch = 0, cameraTimer = null, decoderPromise = null;
function decoderReady() {
  if (typeof window.jsQR === 'function') return Promise.resolve();
  if (!decoderPromise) decoderPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    const timeout = setTimeout(() => {script.remove(); decoderPromise=null; reject(Error('识别组件加载超时，请重试'));}, 15000);
    script.src = '/static/vendor/jsqr/jsQR-1.4.0.js';
    script.onload = () => {clearTimeout(timeout); resolve();};
    script.onerror = () => {clearTimeout(timeout); script.remove(); decoderPromise=null; reject(Error('识别组件加载失败，请重试'));};
    document.head.append(script);
  });
  return decoderPromise;
}
function stopCamera() {
  ++cameraEpoch;
  clearTimeout(cameraTimer);
  if (cameraStream) cameraStream.getTracks().forEach(track => track.stop());
  cameraStream = null;
  $('cameraVideo').srcObject = null;
  $('cameraVideo').hidden = true;
  $('cameraStop').hidden = true;
  $('cameraStart').disabled = false;
}
async function acceptShelf(text) {
  const mold = moldIdentity(text);
  if (mold) {
    stopCamera();
    $('cameraStatus').textContent = '已识别模具，正在打开当前 ERP 信息';
    location.assign(mold);
    return true;
  }
  const target = shelfIdentity(text);
  if (!target) {
    const route = erpMobileRoute(text);
    if (!route) { $('cameraStatus').textContent='请扫描本 ERP 的手机业务二维码'; return false; }
    stopCamera();
    $('cameraStatus').textContent = '已识别，正在打开对应手机页面';
    location.assign(route);
    return true;
  }
  stopCamera();
  id = target.id; product = target.product;
  $('cameraStatus').textContent = '已识别，正在读取货位库存';
  $('cameraStart').textContent = '继续扫码';
  await load();
  $('cameraStatus').textContent = '本次识别完成，可查看下方结果或继续扫码';
  return true;
}
function scanFrame(epoch) {
  if (epoch !== cameraEpoch || !cameraStream) return;
  try {
    const video=$('cameraVideo'), canvas=$('cameraCanvas');
    if (video.readyState >= 2 && video.videoWidth) {
      const scale = Math.min(1, 800 / video.videoWidth);
      canvas.width=Math.round(video.videoWidth*scale); canvas.height=Math.round(video.videoHeight*scale);
      const context=canvas.getContext('2d', {willReadFrequently:true});
      context.drawImage(video,0,0,canvas.width,canvas.height);
      const pixels=context.getImageData(0,0,canvas.width,canvas.height);
      const result=window.jsQR(pixels.data,pixels.width,pixels.height,{inversionAttempts:'attemptBoth'});
      if (result && (shelfIdentity(result.data) || moldIdentity(result.data) || erpMobileRoute(result.data))) { void acceptShelf(result.data); return; }
      if (result) $('cameraStatus').textContent='未识别为本 ERP 的手机业务二维码，请核对标签';
    }
    cameraTimer=setTimeout(()=>scanFrame(epoch),180);
  } catch (_) {stopCamera(); $('cameraStatus').textContent='摄像头画面读取失败，请重新开启';}
}
async function startCamera() {
  if (!window.isSecureContext) { $('cameraHttps').hidden=false; return; }
  if (!navigator.mediaDevices?.getUserMedia) { $('cameraStatus').textContent='此浏览器不能开启摄像头，请用 Safari 打开本页'; return; }
  stopCamera();
  const epoch=cameraEpoch;
  $('cameraStart').disabled=true; $('cameraStop').hidden=false;
  $('cameraStatus').textContent='请允许使用摄像头，首次需加载识别组件…';
  // Request permission directly from the user's tap, before any network wait.
  const media=navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:{ideal:'environment'},width:{ideal:1280},height:{ideal:720}}}).then(stream=>{
    if(epoch!==cameraEpoch) {stream.getTracks().forEach(track=>track.stop()); return null;}
    cameraStream=stream; return stream;
  });
  try {
    const [stream]=await Promise.all([media,decoderReady()]);
    if(epoch!==cameraEpoch || !stream)return;
    const video=$('cameraVideo'); video.srcObject=stream; video.hidden=false;
    await video.play();
    if(epoch!==cameraEpoch)return;
    $('cameraStatus').textContent='请将货位或模具二维码放入画面';
    scanFrame(epoch);
  } catch(e) {
    if(epoch!==cameraEpoch)return;
    stopCamera();
    $('cameraStatus').textContent = e.name==='NotAllowedError' ? '摄像头未获允许。请在 Safari 网站设置中允许摄像头，再点开启。' : e.name==='NotFoundError' ? '没有找到摄像头' : e.name==='NotReadableError' ? '摄像头被占用，请关闭其他相机应用后重试' : e.message || '开启失败，请重试';
  }
}
$('cameraStart').onclick=startCamera;
$('cameraStop').onclick=()=>{stopCamera(); $('cameraStatus').textContent='摄像头已关闭';};
// Photo decoding also works on LAN HTTP: pixels never leave this phone.
$('cameraPhoto').onchange=async event=>{
  const file=event.target.files?.[0];
  if(!file)return;
  stopCamera();
  const epoch=cameraEpoch;
  let objectUrl;
  try {
    if(!file.type.startsWith('image/') || file.size>20*1024*1024)throw Error('请选择小于20MB的二维码照片');
    $('cameraStatus').textContent='正在识别照片…';
    await decoderReady();
    objectUrl=URL.createObjectURL(file);
    const picture=new Image();
    await new Promise((resolve,reject)=>{picture.onload=resolve;picture.onerror=()=>reject(Error('照片无法读取，请重新拍摄'));picture.src=objectUrl;});
    if(epoch!==cameraEpoch)return;
    const canvas=$('cameraCanvas'), context=canvas.getContext('2d',{willReadFrequently:true});
    let result;
    for(const edge of [1200,2400]){
      const scale=Math.min(1,edge/Math.max(picture.naturalWidth,picture.naturalHeight));
      canvas.width=Math.max(1,Math.round(picture.naturalWidth*scale));
      canvas.height=Math.max(1,Math.round(picture.naturalHeight*scale));
      context.drawImage(picture,0,0,canvas.width,canvas.height);
      const pixels=context.getImageData(0,0,canvas.width,canvas.height);
      result=window.jsQR(pixels.data,pixels.width,pixels.height,{inversionAttempts:'attemptBoth'});
      if(result)break;
    }
    if(!result)throw Error('未找到二维码，请靠近标签重新拍摄');
    await acceptShelf(result.data);
  }catch(e){if(epoch===cameraEpoch)$('cameraStatus').textContent=e.message || '照片识别失败，请重试';}
  finally{if(objectUrl)URL.revokeObjectURL(objectUrl);event.target.value='';}
};
window.addEventListener('pagehide',stopCamera);
document.addEventListener('visibilitychange',()=>{if(document.hidden){stopCamera();$('cameraStatus').textContent='摄像头已暂停，返回后请点开启';}});
$('message').textContent='扫码后在这里显示货位库存';
$('refresh').disabled=true;
if (!window.isSecureContext) $('cameraHttps').hidden=false;
