import type { AssetTemplate, Layout, LayoutFeature, LayoutSummary, Pallet, Placement, ProductionProjectionMapping, ProductionProjectionResponse, Rack } from "./types";

const editorToken = import.meta.env.VITE_EDITOR_TOKEN || "local-mvp-token";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.method && init.method !== "GET") {
    headers.set("X-Editor-Token", editorToken);
  }
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(detail.detail || "请求失败");
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  listLayouts: () => request<LayoutSummary[]>("/api/layouts"),
  getLayout: (id: string) => request<Layout>(`/api/layouts/${id}`),
  importDxf: (form: FormData) =>
    request<Layout>("/api/layouts/import-dxf", { method: "POST", body: form }),
  listAssets: () => request<AssetTemplate[]>("/api/assets"),
  createAsset: (form: FormData) =>
    request<AssetTemplate>("/api/assets", { method: "POST", body: form }),
  createPlacement: (layoutId: string, body: object) =>
    request<Placement>(`/api/layouts/${layoutId}/placements`, {
      method: "POST",
      body: JSON.stringify(body)
    }),
  updatePlacement: (id: string, body: object) =>
    request<Placement>(`/api/placements/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body)
    }),
  deletePlacement: (id: string) =>
    request<void>(`/api/placements/${id}`, { method: "DELETE" }),
  createRack: (layoutId: string, body: object) =>
    request<Rack>(`/api/layouts/${layoutId}/racks`, { method: "POST", body: JSON.stringify(body) }),
  updateRack: (id: string, body: object) =>
    request<Rack>(`/api/racks/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  confirmRack: (id: string, version: number) =>
    request<Rack>(`/api/racks/${id}/confirm?version=${version}`, { method: "POST" }),
  deleteRack: (id: string) => request<void>(`/api/racks/${id}`, { method: "DELETE" }),
  createPallet: (layoutId: string, body: object) =>
    request<Pallet>(`/api/layouts/${layoutId}/pallets`, { method: "POST", body: JSON.stringify(body) }),
  updatePallet: (id: string, body: object) =>
    request<Pallet>(`/api/pallets/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deletePallet: (id: string) => request<void>(`/api/pallets/${id}`, { method: "DELETE" }),
  getProductionProjections: (layoutId: string) =>
    request<ProductionProjectionResponse>(`/api/layouts/${layoutId}/production-projections`),
  bindProductionProjection: (layoutId: string, taskId: number, body: object) =>
    request<ProductionProjectionMapping>(`/api/layouts/${layoutId}/production-projections/${taskId}`, {
      method: "PUT",
      body: JSON.stringify(body)
    }),
  deleteProductionProjection: (layoutId: string, taskId: number, version: number) =>
    request<void>(`/api/layouts/${layoutId}/production-projections/${taskId}?version=${version}`, {
      method: "DELETE"
    }),
  createFeature: (layoutId: string, body: object) =>
    request<LayoutFeature>(`/api/layouts/${layoutId}/features`, { method: "POST", body: JSON.stringify(body) }),
  updateFeature: (id: string, body: object) =>
    request<LayoutFeature>(`/api/features/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  confirmFeature: (id: string, version: number) =>
    request<LayoutFeature>(`/api/features/${id}/confirm?version=${version}`, { method: "POST" }),
  deleteFeature: (id: string) => request<void>(`/api/features/${id}`, { method: "DELETE" })
};
