import type { Layout } from "./types";

export type FeatureKind = "zone" | "aisle" | "no_go" | "structure";

export function nextFeatureCode(
  layout: Pick<Layout, "floor_code" | "features"> | null,
  kind: FeatureKind,
  subtype: string,
  reservedCodes?: string[]
): string;

export function resolveFeatureCode(
  layout: Pick<Layout, "floor_code" | "features"> | null,
  kind: FeatureKind,
  subtype: string,
  preferredCode: string,
  reservedCodes?: string[]
): string;
