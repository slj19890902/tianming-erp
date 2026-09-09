// Camera frames stay on-device; only the extracted location is sent to our existing API.
function shelfIdentity(text) {
  try {
    const url = new URL(text, location.origin);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
    const hosts = new Set([location.hostname, 'tianmingerp0909.share.zrok.io', '192.168.3.80']);
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
  const target = shelfIdentity(text);
  if (!target) { $('cameraStatus').textContent='请扫描本 ERP 的货位或货位产品二维码'; return false; }
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
      if (result && shelfIdentity(result.data)) { void acceptShelf(result.data); return; }
      if (result) $('cameraStatus').textContent='这不是本 ERP 的货位二维码，请换一个';
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
    $('cameraStatus').textContent='请将货位二维码放入画面';
    scanFrame(epoch);
  } catch(e) {
    if(epoch!==cameraEpoch)return;
    stopCamera();
    $('cameraStatus').textContent = e.name==='NotAllowedError' ? '摄像头未获允许。请在 Safari 网站设置中允许摄像头，再点开启。' : e.name==='NotFoundError' ? '没有找到摄像头' : e.name==='NotReadableError' ? '摄像头被占用，请关闭其他相机应用后重试' : e.message || '开启失败，请重试';
  }
}
$('cameraStart').onclick=startCamera;
$('cameraStop').onclick=()=>{stopCamera(); $('cameraStatus').textContent='摄像头已关闭';};
window.addEventListener('pagehide',stopCamera);
document.addEventListener('visibilitychange',()=>{if(document.hidden){stopCamera();$('cameraStatus').textContent='摄像头已暂停，返回后请点开启';}});
$('message').textContent='扫码后在这里显示货位库存';
$('refresh').disabled=true;
if (!window.isSecureContext) $('cameraHttps').hidden=false;
