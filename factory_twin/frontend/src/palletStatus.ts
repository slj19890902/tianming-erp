import type { Pallet } from "./types";

export const PALLET_VISUAL_STATES: Array<{
  value: Pallet["visual_status"];
  label: string;
  color: string;
  description: string;
}> = [
  { value: "empty", label: "空栈板", color: "#8b6f47", description: "未承载物料，可回收或待使用" },
  { value: "waiting", label: "待生产", color: "#2563eb", description: "已进入现场，等待上机" },
  { value: "in_process", label: "生产周转中", color: "#f59e0b", description: "工序之间的临时周转" },
  { value: "completed", label: "完工待转运", color: "#16a34a", description: "已完工，等待搬运或送货" },
  { value: "abnormal", label: "异常暂存", color: "#dc2626", description: "需要人工复核或隔离处理" }
];

export function palletStatusInfo(status: Pallet["visual_status"] | string) {
  return PALLET_VISUAL_STATES.find((item) => item.value === status) || PALLET_VISUAL_STATES[0];
}
