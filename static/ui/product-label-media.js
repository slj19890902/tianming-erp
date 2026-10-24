(function (root) {
  "use strict";
  const PRESETS = Object.freeze({
    roll_40x30: {name:"40 × 30 mm", width_mm:40, height_mm:30},
    roll_40x80: {name:"40 × 80 mm（竖向）", width_mm:40, height_mm:80},
    roll_80x40: {name:"80 × 40 mm（横向）", width_mm:80, height_mm:40},
    roll_65x45: {name:"65 × 45 mm", width_mm:65, height_mm:45},
    a4_4up: {name:"A4 四格（2 × 2）", width_mm:210, height_mm:297},
    custom: {name:"自定义标签", width_mm:40, height_mm:30},
  });
  const round = n => Math.round(n * 1000) / 1000;
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const fields = ["margin_top_mm", "margin_right_mm", "margin_bottom_mm", "margin_left_mm", "gap_x_mm", "gap_y_mm"];
  function number(value, name, min, max) {
    if (typeof value === "boolean" || value === null || value === "" || !Number.isFinite(Number(value))) throw new Error(`${name}须填写有效数字`);
    const n = Number(value);
    if (n < min || n > max) throw new Error(`${name}须在 ${min}～${max} 毫米之间`);
    return round(n);
  }
  function normalize(raw = {}) {
    const preset = raw.preset || "roll_40x30", definition = PRESETS[preset];
    if (!definition) throw new Error("请选择有效的标签纸规格");
    const m = {schema_version:1, preset};
    m.width_mm = preset === "custom" ? number(raw.width_mm, "纸张宽度", 20, 300) : definition.width_mm;
    m.height_mm = preset === "custom" ? number(raw.height_mm, "纸张高度", 20, 420) : definition.height_mm;
    for (const key of fields) m[key] = number(raw[key] ?? 0, "边距或间距", 0, 50);
    m.rows = m.columns = preset === "a4_4up" ? 2 : 1;
    m.start_position = Number(raw.start_position ?? 1);
    if (!Number.isInteger(m.start_position) || m.start_position < 1 || m.start_position > m.rows * m.columns) throw new Error("请选择有效的首张标签位置");
    if (m.columns === 1 && (m.gap_x_mm || m.gap_y_mm)) throw new Error("单张标签无需设置格间距");
    m.label_width_mm = Math.floor((m.width_mm - m.margin_left_mm - m.margin_right_mm - (m.columns-1)*m.gap_x_mm) / m.columns * 1000 + 1e-8) / 1000;
    m.label_height_mm = Math.floor((m.height_mm - m.margin_top_mm - m.margin_bottom_mm - (m.rows-1)*m.gap_y_mm) / m.rows * 1000 + 1e-8) / 1000;
    if (m.label_width_mm < 20 || m.label_height_mm < 20) throw new Error("边距或间距过大，标签可打印区域须至少为20 × 20毫米");
    return m;
  }
  function pageCount(count, media) { const m = normalize(media); return count > 0 ? Math.ceil((count + m.start_position - 1) / (m.rows*m.columns)) : 0; }
  function pages(labels, media, html) {
    const m = normalize(media), capacity = m.rows*m.columns, result = [];
    for (let i=0, slot=0; i<labels.length; slot+=capacity) {
      const cells = [];
      for (let pos=0; pos<capacity; pos++) {
        if (slot === 0 && pos < m.start_position-1 || i >= labels.length) cells.push('<div class="media-empty" aria-hidden="true"></div>');
        else cells.push(html(labels[i++]));
      }
      result.push(`<section class="label-media-sheet" aria-label="第${result.length+1}张纸">${cells.join("")}</section>`);
    }
    return result.join("");
  }
  function css(media) {
    const m=normalize(media);
    return `@page { size:${m.width_mm}mm ${m.height_mm}mm; margin:0; }
      :root { --label-width:${m.label_width_mm}mm; --label-height:${m.label_height_mm}mm; }
      .label-media-sheet { box-sizing:border-box; display:grid; grid-template-columns:repeat(${m.columns},${m.label_width_mm}mm); grid-template-rows:repeat(${m.rows},${m.label_height_mm}mm); column-gap:${m.gap_x_mm}mm; row-gap:${m.gap_y_mm}mm; padding:${m.margin_top_mm}mm ${m.margin_right_mm}mm ${m.margin_bottom_mm}mm ${m.margin_left_mm}mm; width:${m.width_mm}mm; height:${m.height_mm}mm; background:white; box-shadow:0 2px 10px #0002; }
      .label-media-sheet > .label-card { width:${m.label_width_mm}mm; height:${m.label_height_mm}mm; margin:0; box-shadow:none; break-after:auto; page-break-after:auto; }
      .label-media-sheet .media-text { position:absolute; display:flex; align-items:center; box-sizing:border-box; overflow:hidden; padding:.2mm; }
      .label-media-sheet .media-value { display:block; max-width:100%; white-space:normal; overflow-wrap:anywhere; word-break:break-all; line-height:1.15; }
      @media print { html,body { width:${m.width_mm}mm; margin:0; padding:0; } .media-controls { display:none !important; } #labelList { display:block; width:${m.width_mm}mm; margin:0; padding:0; } .label-media-sheet { margin:0; box-shadow:none; break-inside:avoid; page-break-inside:avoid; break-after:page; page-break-after:always; } .label-media-sheet:last-child { break-after:auto; page-break-after:auto; } .label-media-sheet > .label-card { break-after:auto; page-break-after:auto; } }
    `;
  }
  const defaults = [
    {id:"customer_short_name",x_mm:.8,y_mm:.6,width_mm:38.4,height_mm:4.2,font_size_mm:3.65,font_weight:900,text_align:"center",visible:true},
    {id:"product_code",x_mm:.8,y_mm:5,width_mm:38.4,height_mm:5.9,font_size_mm:3.6,font_weight:900,text_align:"center",visible:true},
    {id:"product_name",x_mm:.8,y_mm:11.2,width_mm:38.4,height_mm:4.2,font_size_mm:2.85,font_weight:800,text_align:"center",visible:true},
    {id:"specification",x_mm:.8,y_mm:15.6,width_mm:38.4,height_mm:4.2,font_size_mm:2.85,font_weight:800,text_align:"center",visible:true},
    {id:"quantity",x_mm:.8,y_mm:20.2,width_mm:38.4,height_mm:9,font_size_mm:6.1,font_weight:900,text_align:"center",visible:true},
  ];
  function elements(label, packageData, media) {
    const m=normalize(media), sx=m.label_width_mm/40, sy=m.label_height_mm/30;
    let source=packageData.label_layout?.layout?.elements || defaults;
    if (label.label_profile === "customer-drawing-code-quantity-v1") {
      source=[{...defaults[0],height_mm:5}, {...defaults[1],y_mm:6,height_mm:6}, {...defaults[2],id:"drawing_number",y_mm:12.5,height_mm:7,font_size_mm:3.2}, defaults[4]];
    }
    return source.filter(e=>e.visible).map(e=> {
      if (e.kind === "qr") throw new Error("该二维码布局尚无产品数据源，请先调整布局");
      const factor=Math.min(sx*1.3, sy, Math.pow(sx*sy,.4));
      return {...e,x_mm:round(e.x_mm*sx),y_mm:round(e.y_mm*sy),width_mm:round(e.width_mm*sx),height_mm:round(e.height_mm*sy),font_size_mm:round(Math.max(1.8,Math.min(16,e.font_size_mm*factor)))};
    });
  }
  function labelHtml(label, packageData, media) {
    const parts=elements(label,packageData,media).map(e=> {
      let text=label[e.id] ?? "";
      if (e.id === "customer_short_name") text=label.customer_short_name || label.customer_name || "";
      if (e.id === "drawing_number") text=`图号 ${label.drawing_number || "待完善"}`;
      if (e.id === "quantity") {
        const suffix=Object.prototype.hasOwnProperty.call(e,"fixed_suffix") ? e.fixed_suffix : `${label.unit_label || "只"}/捆`;
        const suffixText=String(suffix);
        text=`${label.quantity}${/^(只|片|套)(\/捆)?$/.test(suffixText) ? suffixText.replace(/^(只|片|套)/,label.unit_label || "只") : suffixText}`;
      }
      const align=["left","center","right"].includes(e.text_align) ? e.text_align : "center";
      return `<div class="media-text" style="left:${e.x_mm}mm;top:${e.y_mm}mm;width:${e.width_mm}mm;height:${e.height_mm}mm;justify-content:${{left:"flex-start",center:"center",right:"flex-end"}[align]};text-align:${align};font-size:${e.font_size_mm}mm;font-weight:${e.font_weight || 800}"><strong class="media-value" data-min-font-mm="1.8" title="${escape(text)}">${escape(text)}</strong></div>`;
    });
    return `<article class="label-card layout-driven media-label" aria-label="产品标签 ${escape(label.label_number)}/${escape(label.label_count)}">${parts.join("")}</article>`;
  }
  const api=Object.freeze({PRESETS,normalize,pageCount,pages,css,elements,labelHtml});
  root.ERPProductLabelMedia=api;
  if (typeof module === "object" && module.exports) module.exports=api;
})(typeof window === "object" ? window : globalThis);
