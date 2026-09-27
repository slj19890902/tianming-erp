const params = new URLSearchParams(location.search);
const ids = (params.get('location_ids') || params.get('location_id') || '').split(',');
const lot = params.get('lot_id');
const h = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const $ = id => document.getElementById(id);
let busy = false;
function label(row) {
  const heading = `<div><h1 class="fit">${h(row.title)}</h1><strong class="fit">${h(row.position)}</strong></div><img src="${h(row.qr_data_url)}" alt="手机查询二维码">`;
  if (!row.product) return `<article class="label"><div class="location">${heading}</div></article>`;
  const p = row.product;
  const productHeading = `<div class="product-address"><div class="product-title-row"><h1 class="fit">${h(row.print_title || row.title)}</h1><strong>${h(row.print_position || row.position)}</strong></div><div class="product-floor fit">${h(row.print_floor || '')}</div></div><img src="${h(row.qr_data_url)}" alt="手机查询二维码">`;
  return `<article class="label"><div class="product-head">${productHeading}</div><div class="fields">${[['客户',p.customer],['存货编码',p.code],['产品名称',p.name],['规格',p.specification]].map(([key,value]) => `<div class="field"><span>${key}：</span><span class="value fit ${key==='存货编码'?'code':key==='客户'?'customer':''}">${h(value)}</span></div>`).join('')}</div></article>`;
}
async function load() {
  if (busy) return false;
  busy = true; $('print').disabled = true; $('retry').disabled = true; $('labels').innerHTML = '';
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
    $('labels').innerHTML = rows.map(row => `<section class="label-page">${label(row)}</section>`).join('');
    await document.fonts.ready;
    await Promise.all([...document.querySelectorAll('img')].map(img => img.decode()));
    for (const node of document.querySelectorAll('.fit')) {
      let size = parseFloat(getComputedStyle(node).fontSize);
      while (node.scrollWidth > node.clientWidth + 1 && size > 10) { size -= .5; node.style.fontSize = size+'px'; }
      if (node.scrollWidth > node.clientWidth + 1) throw Error('文字过长，请核对简称或名称');
    }
    for (const node of document.querySelectorAll('.label')) if(node.scrollHeight > node.clientHeight + 1) throw Error('标签内容超出尺寸');
    $('message').textContent = `40×80mm纵向纸型 · ${rows.length}张 · 缩放100%、无边距，不再手动旋转`;
    $('print').disabled = false;
    return true;
  } catch(e) { $('labels').innerHTML = ''; $('message').textContent = e.message || '读取失败，请重试'; return false; }
  finally {busy=false; $('retry').disabled=false;}
}
$('retry').onclick = load;
$('print').onclick = async () => { if (await load()) window.print(); };
load();
