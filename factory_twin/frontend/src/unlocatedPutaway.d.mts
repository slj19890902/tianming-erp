type Pallet = { pallet_id: number; version: number; move_eligible?: boolean | null; move_block_reason?: string | null; items: { lot_id: number }[] };
type Location = { location_id: number; pallets?: Pallet[]; pallet?: Pallet | null };
export function unlocatedPalletChoice<L extends Location>(item: { location_id?: number | null; lot_id?: number | null }, locations: L[]): { location: L; pallet: NonNullable<L["pallet"]>; error?: never } | { error: string; location?: never; pallet?: never };
