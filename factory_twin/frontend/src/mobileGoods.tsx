import {createRoot} from "react-dom/client";
import {UnassignedFinishedEntry} from "./UnassignedFinishedEntry";
import {WarehouseGoods} from "./WarehouseGoods";
import "./mobileGoods.css";
type Config={unassigned?:boolean;locationId:number;layoutVersion:number;raw:boolean;canSave:boolean;onBusyChange:(busy:boolean)=>void;onSaved:()=>Promise<unknown>};
(window as unknown as Window & {mountMobileGoods:(node:HTMLElement,config:Config)=>()=>void}).mountMobileGoods=(node,config)=>{
  const root=createRoot(node);root.render(config.unassigned?<UnassignedFinishedEntry {...config}/>:<WarehouseGoods {...config}/>);return()=>root.unmount();
};
