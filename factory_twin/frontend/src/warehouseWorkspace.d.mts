export type MoveLocationState = {kind:'blocked'|'source'|'target'|'empty'|'occupied';selectable:boolean;label:string;reason:string};
export function moveLocationState(location:{location_id?:number;occupancy_status?:string;map_rack_id?:string|null;address_kind?:string}|null|undefined,source:{operation:string;source_location_id?:number}|null,eligibleIds:number[],targetId:number|string|null):MoveLocationState;
export function areaSortKey(name:unknown):string;
