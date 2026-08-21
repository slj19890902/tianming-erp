export function moldRackDraftWorkflowState({
  busy = false,
  hasDraft = false,
  status = "none",
  hasUnsavedInput = false
} = {}) {
  const save = {
    disabled: Boolean(busy),
    title: busy ? "层格操作处理中，请稍候" : "保存当前货架层格到当前楼层草稿"
  };
  const validate = {
    disabled: Boolean(busy || !hasDraft || hasUnsavedInput),
    title: busy
      ? "层格操作处理中，请稍候"
      : hasUnsavedInput
        ? "请先完成第①步保存当前输入"
        : !hasDraft
          ? "请先完成第①步生成当前楼层草稿"
          : "只校验当前楼层草稿"
  };
  const publish = {
    disabled: Boolean(busy || !hasDraft || status !== "validated" || hasUnsavedInput),
    title: busy
      ? "层格操作处理中，请稍候"
      : hasUnsavedInput
        ? "请先完成第①步保存当前输入"
        : !hasDraft
          ? "请先完成第①步生成当前楼层草稿"
          : status !== "validated"
            ? "请先完成第②步校验当前楼层草稿"
            : "只发布当前楼层，其他楼层草稿会保留"
  };
  return { save, validate, publish };
}
