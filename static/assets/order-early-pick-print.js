(function () {
  "use strict";
  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, ch =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const number = value => {
    if (value === null || value === undefined || value === "") return "待核对";
    const n = Number(value);
    return Number.isFinite(n) ? (Number.isInteger(n) ? String(n) : n.toFixed(3)) : "待核对";
  };
  const time = value => new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
  function render(data) {
    const e = escapeHtml;
    return `<h1>提前备货单</h1><div class="meta">
      <div>客户：${e(data.customer_name)}</div><div>要求交货：${e(data.delivery_date || "待填写")}</div>
      <div>订单：${e(data.order_number)}</div><div>客户单号：${e(data.customer_po || "原表未提供")}</div>
      <div>来源：${e(data.source_name)}</div><div>库存核对时间：${e(time(data.generated_at))}</div></div>
      <table><colgroup><col style="width:5%"><col style="width:23%"><col style="width:25%"><col style="width:12%"><col style="width:12%"><col style="width:12%"><col style="width:11%"></colgroup>
      <thead><tr><th>序号</th><th>存货编码／产品</th><th>指定拿货位置</th><th>需求数量</th><th>当前可拿</th><th>待补数量</th><th>实拿数量</th></tr></thead><tbody>
      ${(data.rows || []).map(row => `<tr><td>${e(row.sequence)}</td>
        <td><div class="code">${e(row.product_code)}</div><div class="sub">${e(row.product_name)}</div><div class="sub">${e(row.unit)}</div></td>
        <td><strong>${e(row.requested_location || "未填写")}</strong>
          ${(row.locations || []).map(loc => `<div class="sub">${e(loc.location)} · ${e(number(loc.quantity))} ${e(row.unit)}<br>${e(loc.kind)}${loc.physical_quantity !== loc.quantity ? ` · 实物 ${e(number(loc.physical_quantity))}（按冻结换算）` : ""}</div>`).join("")}
          ${(row.notes || []).map(note => `<div class="sub warning">${e(note)}</div>`).join("")}</td>
        <td class="num">${e(number(row.order_quantity))}${row.delivered_quantity ? `<div class="sub">已送 ${e(number(row.delivered_quantity))}<br>待备 ${e(number(row.pending_quantity))}</div>` : ""}</td>
        <td class="num">${e(number(row.available_quantity))}</td><td class="num">${e(number(row.shortage_quantity))}</td>
        <td><span class="write"></span></td></tr>`).join("")}</tbody></table>
      <p class="notice">${e(data.notice)}<br>数量按各行单位核对；可用成品参考尚未预留，实际拿货前请核对。位置待核对的行不推算可拿数量。</p>
      <div class="sign"><span>备货人：____________</span><span>核对人：____________</span><span>日期：____________</span></div>`;
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { render, number };
    return;
  }
  const orderId = new URLSearchParams(location.search).get("order_id");
  const sheet = document.getElementById("sheet"), state = document.getElementById("state");
  const print = document.getElementById("print"), refresh = document.getElementById("refresh");
  let loading = false;
  async function load(andPrint = false) {
    if (loading) return;
    loading = true;
    print.disabled = refresh.disabled = true;
    sheet.hidden = true;
    state.hidden = false;
    state.textContent = "正在读取订单及当前库存…";
    try {
      if (!/^[1-9]\d*$/.test(orderId || "")) throw new Error("缺少有效订单，请从已保存订单打开。");
      const response = await fetch(`/api/orders/${encodeURIComponent(orderId)}/early-pick`, {
        credentials: "include", cache: "no-store",
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(response.status === 401 ? "登录已失效，请返回 ERP 登录后刷新。" :
        (typeof data.detail === "string" ? data.detail : "读取失败，请核对权限后重试。"));
      if (!Array.isArray(data.rows)) throw new Error("返回内容不完整，请重新登录 ERP 后刷新。");
      sheet.innerHTML = render(data);
      sheet.hidden = false;
      state.hidden = true;
      print.disabled = false;
      if (andPrint) window.print();
    } catch (error) {
      sheet.innerHTML = "";
      state.textContent = error.message || "读取失败，请重试。";
    } finally {
      loading = false;
      refresh.disabled = false;
    }
  }
  print.addEventListener("click", () => load(true));
  refresh.addEventListener("click", () => load());
  load();
})();
