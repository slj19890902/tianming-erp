import {createRoot} from "react-dom/client";
import {WarehouseGoods} from "./WarehouseGoods";
import "./mobileGoods.css";
type Config={locationId:number;layoutVersion:number;raw:boolean;canSave:boolean;onBusyChange:(busy:boolean)=>void;onSaved:()=>Promise<unknown>};
(window as unknown as Window & {mountMobileGoods:(node:HTMLElement,config:Config)=>()=>void}).mountMobileGoods=(node,config)=>{
  const root=createRoot(node);root.render(<WarehouseGoods {...config}/>);return()=>root.unmount();
};
