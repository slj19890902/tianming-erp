/* Existing product structure is evidence, not a set of new-product defaults. */
(function(root) {
  "use strict";
  const fields = ["box_style", "splice_mode", "pieces_per_box", "flap_mm", "production_process"];
  const empty = value => value === null || value === undefined || value === "";
  const paperStructure = form => form.supply_mode !== "external_purchase"
    && !form.is_virtual_composite_parent && form.box_style !== "BOM组合";
  function initialize(form, detail) {
    if (!form.id) return form;
    form._structure_original = Object.fromEntries(fields.map(key => [key, detail[key] ?? null]));
    form._structure_edit_fields = [];
    form._structure_last_box_style = form._structure_original.box_style;
    form._structure_preserve = paperStructure(form);
    if (form._structure_preserve) Object.assign(form, form._structure_original);
    return form;
  }
  function mark(form, names) {
    if (!form.id) return;
    const selected = new Set(form._structure_edit_fields || []);
    names.forEach(key => { if (fields.includes(key)) selected.add(key); });
    form._structure_edit_fields = fields.filter(key => selected.has(key));
  }
  function reset(form, key) {
    if (!form.id || !fields.includes(key)) return;
    const names = ["splice_mode", "pieces_per_box"].includes(key) ? ["splice_mode", "pieces_per_box"] : [key];
    names.forEach(name => { form[name] = null; });
    mark(form, names);
    if (key === "box_style") form._structure_last_box_style = null;
    if (key === "production_process") {
      form._production_processes = [];
      form._production_process_legacy = "";
      form._structure_process_reset = true;
      form._joining_choice_manual = false;
      form.unit_needs_review = true;
    }
  }
  function applyPayload(form, payload) {
    if (!form.id) return payload;
    if (!form._structure_original) throw new Error("产品结构原值缺失，请重新打开产品后保存");
    const edited = form._structure_edit_fields || [];
    payload.structure_edit_fields = [...edited];
    if (form._structure_preserve && paperStructure(form)) {
      fields.forEach(key => {
        if (!edited.includes(key)) payload[key] = form._structure_original[key];
        else if (key !== "production_process") payload[key] = empty(form[key]) ? null : form[key];
      });
      if (edited.includes("production_process") && form._structure_process_reset) payload.production_process = null;
    }
    Object.keys(payload).filter(key => key.startsWith("_structure_")).forEach(key => delete payload[key]);
    return payload;
  }
  function canRecommend(form, rule) {
    if (!form.id) return true;
    if (!rule || empty(form.box_style)) return false;
    // No recommendation may infer missing physical structure from customer sizes.
    if (rule.supports_splice && (!["single", "double"].includes(form.splice_mode)
        || Number(form.pieces_per_box) !== (form.splice_mode === "double" ? 2 : 1))) return false;
    if (rule.uses_flap && (empty(form.flap_mm) || !(Number(form.flap_mm) > 0))) return false;
    return true;
  }
  function chooseBoxStyle(form, previousRule, rule) {
    if (!form.id) return false;
    const previous = form._structure_last_box_style;
    form._structure_last_box_style = form.box_style;
    mark(form, ["box_style"]);
    const sameRule = previousRule && rule && previousRule.code === rule.code;
    // Compatibility names change the current label only; they do not establish structure.
    if (sameRule || empty(form.box_style)
        || form.box_style === previous && !["BOM组合", "其他"].includes(form.box_style)) return true;
    mark(form, fields);
    form.splice_mode = rule?.supports_splice && form.splice_mode === "double" ? "double" : "single";
    form.pieces_per_box = form.splice_mode === "double" ? 2 : 1;
    form.flap_mm = rule?.uses_flap ? (empty(form.flap_mm) ? 30 : form.flap_mm) : null;
    // Explicit box changes retain an unknown joining process until it is chosen.
    form._structure_process_reset = empty(form.production_process) && !(form._production_processes || []).length;
    return false;
  }
  const api = {fields, initialize, mark, reset, applyPayload, canRecommend, chooseBoxStyle};
  root.ERPProductStructure = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
