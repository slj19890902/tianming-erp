const params = new URLSearchParams(location.search);
const ids = (params.get('location_ids') || params.get('location_id') || '').split(',');
const lot = params.get('lot_id');
const h = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const $ = id => document.getElementById(id);
const productAddress = row => [row.compact_title ?? row.print_title ?? row.title, row.compact_position ?? row.print_position ?? row.position].filter(Boolean).join(' ');
let busy = false;
$('contentControl').hidden = Boolean(lot);
function label(row) {
  if (!lot && $('labelContent').value === 'rack') return `<article class="label"><div class="rack-only"><h1 class="fit">${h(row.rack_label)}</h1><img src="${h(row.rack_qr_data_url)}" alt="扫码查看整架"></div></article>`;
  const heading = `<div><h1 class="fit">${h(row.title)}</h1><strong class="fit">${h(row.position)}</strong></div><img src="${h(row.qr_data_url)}" alt="手机查询二维码">`;
  if (!row.product) return `<article class="label"><div class="location">${heading}</div></article>`;
  const p = row.product;
  return `<article class="label"><div class="product-layout"><div class="product-details"><div class="product-head"><h1 class="fit">${h(productAddress(row))}</h1></div><div class="fields">${[['客户',p.customer],['编码',p.code],['品名',p.name],['规格',p.specification]].map(([key,value]) => `<div class="field"><span>${key}：</span><span class="value fit ${key==='编码'?'code':key==='客户'?'customer':''}">${h(value)}</span></div>`).join('')}</div></div><img class="product-qr" src="${h(row.qr_data_url)}" alt="手机查询二维码"></div></article>`;
}
// Send one native-size bitmap per physical page. Thermal drivers must not
// independently rotate/vectorize text and the QR image.
async function rasterLabel(row, rackOnly) {
  const canvas = document.createElement('canvas');
  canvas.width = 320; canvas.height = 640; // 40x80 mm at 8 dots/mm (203 dpi)
  const ctx = canvas.getContext('2d');
  if (!ctx) throw Error('浏览器无法生成打印图像，请刷新重试');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.translate(canvas.width, 0); ctx.rotate(Math.PI / 2); ctx.scale(8, 8);
  ctx.fillStyle = '#000'; ctx.textBaseline = 'top';
  function text(value, x, y, width, points, bold=false) {
    value = String(value ?? '');
    let mm = points * 25.4 / 72;
    const setFont = () => { ctx.font = `${bold?'bold ':''}${mm}px Arial,"Microsoft YaHei",sans-serif`; };
    setFont();
    while (ctx.measureText(value).width > width && mm > 2.4) { mm -= .1; setFont(); }
    if (ctx.measureText(value).width > width) throw Error('标签文字过长，请核对名称后打印');
    ctx.fillText(value, x, y);
  }
  const qr = new Image();
  qr.src = rackOnly ? row.rack_qr_data_url : row.qr_data_url;
  await qr.decode();
  ctx.imageSmoothingEnabled = false;
  if (rackOnly) {
    text(row.rack_label, 2, 12, 46, 36, true);
    ctx.drawImage(qr, 50, 6, 28, 28);
  } else if (!row.product) {
    text(row.title, 2, 7, 46, 19, true);
    text(row.position, 2, 23, 46, 20, true);
    ctx.drawImage(qr, 50, 6, 28, 28);
  } else {
    text(productAddress(row), 2, 3, 44, 20, true);
    ctx.drawImage(qr, 48, 5, 30, 30);
    ctx.fillRect(2, 11, 44, .2);
    const p = row.product;
    [['客户',p.customer],['编码',p.code],['品名',p.name],['规格',p.specification]].forEach(([key,value],i) => {
      text(key+'：',2,12.5+i*6,9,9);
      text(value,12,12.5+i*6,34,key==='编码'?16:key==='客户'?14:11.5,i<2);
    });
  }
  const image = new Image(); image.className = 'print-raster';
  image.alt = rackOnly ? row.rack_label+'，整架二维码' : row.title+' '+row.position+'，二维码';
  image.src = canvas.toDataURL('image/png'); await image.decode();
  return image;
}
async function load() {
  if (busy) return false;
  busy = true; $('labelContent').disabled = true; $('print').disabled = true; $('retry').disabled = true; $('labels').innerHTML = '';
  try {
    if (!ids.length || ids.length > 500 || new Set(ids).size !== ids.length || ids.some(id => !/^[1-9]\d*$/.test(id)) || (lot && (!/^[1-9]\d*$/.test(lot) || ids.length !== 1))) throw Error('标签选择无效');
    $('message').textContent = '读取标签…';
    const rows = [];
    // Bound concurrency; never send hundreds of requests simultaneously.
    for (let i=0; i<ids.length; i+=5) rows.push(...await Promise.all(ids.slice(i,i+5).map(async id => {
      const r = await fetch(`/api/warehouse/locations/${id}/mobile-label${lot?'?lot_id='+lot:''}`, {credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(15000)});
      if (r.status === 401) { location.href = '/?next=' + encodeURIComponent(location.pathname + location.search); throw Error('请登录'); }
      const data = await r.json();
      if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : '读取失败');
      return data;
    })));
    let printable = rows;
    if (!lot && $('labelContent').value === 'rack') {
      if (rows.some(row => !row.rack_key || !row.rack_label || !row.rack_qr_data_url)) throw Error('所选位置缺少完整货架身份，请使用每层每格标签');
      printable = [...new Map(rows.map(row => [row.rack_key, row])).values()];
    }
    $('labels').innerHTML = printable.map(row => `<section class="label-page">${label(row)}</section>`).join('');
    await document.fonts.ready;
    await Promise.all([...document.querySelectorAll('img')].map(img => img.decode()));
    for (const node of document.querySelectorAll('.fit')) {
      let size = parseFloat(getComputedStyle(node).fontSize);
      while (node.scrollWidth > node.clientWidth + 1 && size > 10) { size -= .5; node.style.fontSize = size+'px'; }
      if (node.scrollWidth > node.clientWidth + 1) throw Error('文字过长，请核对简称或名称');
    }
    for (const node of document.querySelectorAll('.label')) if(node.scrollHeight > node.clientHeight + 1) throw Error('标签内容超出尺寸');
    const pages = [...document.querySelectorAll('.label-page')];
    for (let i=0; i<printable.length; i++) pages[i].append(await rasterLabel(printable[i], !lot && $('labelContent').value === 'rack'));
    $('message').textContent = `40×80mm纵向纸型 · ${printable.length}张 · 缩放100%、无边距，不再手动旋转`;
    $('print').disabled = false;
    return true;
  } catch(e) { $('labels').innerHTML = ''; $('message').textContent = e.message || '读取失败，请重试'; return false; }
  finally {busy=false; $('labelContent').disabled=false; $('retry').disabled=false;}
}
$('labelContent').onchange = load;
$('retry').onclick = load;
$('print').onclick = async () => { if (await load()) window.print(); };
load();
