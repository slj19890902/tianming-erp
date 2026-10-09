/* Names only: never use these labels to convert quantities or stock identities. */
(function(root) {
  "use strict";
  const countUnits = new Set(["", "只", "片", "套", "个", "件", "PCS", "pcs", "boxes", "pieces", "sets"]);
  function info(product, form = false) {
    const p = product || {};
    if (!form && p.unit_label !== undefined) return {label:p.unit_label, review:!!p.unit_needs_review};
    const raw = String(p.unit || "").trim();
    const fallback = ({boxes:"只",pieces:"片",sets:"套",sheets:"张"})[raw] || raw;
    if (form && p.id && p.unit_needs_review && !p._joining_choice_manual) return {label:fallback,review:true};
    if (p.supply_mode === "external_purchase" || !countUnits.has(raw)) return {label:fallback,review:!raw};
    if (p.is_composite || p.box_style === "BOM组合") return {label:"套",review:false};
    if (p.is_internal_component) return {label:"片",review:false};
    const process = form && Array.isArray(p._production_processes) ? p._production_processes.join(",") : p.production_process;
    const tokens = new Set(String(process || "").split(/[,，、;；\s]+/).filter(Boolean));
    const has = values => values.some(x => tokens.has(x));
    const glue = has(["粘合","粘贴","粘箱","糊箱","糊盒"]), staple = has(["打钉","钉箱","打钉箱","钉合"]);
    const noJoin = has(["无需结合","无需","不需结合","不需要结合"]);
    return (glue || staple) !== noJoin ? {label:noJoin?"片":"只",review:false} : {label:fallback,review:true};
  }
  function pendingProcess(form) {
    const original = form.production_process;
    const tokens = String(original || "").split(/[,，、;；]+/).map(x=>x.trim()).filter(Boolean);
    const mold = (form._production_processes || []).includes("模切");
    if (mold === tokens.includes("模切")) return original;
    return [...tokens.filter(x=>x!=="模切"), ...(mold?["模切"]:[])].join(",") || null;
  }
  const api = {info, pendingProcess, label:(p,form=false)=>info(p,form).label || "单位待完善"};
  root.ERPProductUnits = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
