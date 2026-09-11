import {createRoot} from "react-dom/client";
import {WarehouseGoods} from "./WarehouseGoods";
import "./mobileGoods.css";
const host=window.parent as Window & {
  mobileGoodsConfig:()=>{locationId:number;layoutVersion:number;raw:boolean;canSave:boolean};
  mobileGoodsBusy:(busy:boolean)=>void;mobileGoodsSaved:()=>Promise<unknown>;
};
createRoot(document.getElementById("root")!).render(<WarehouseGoods {...host.mobileGoodsConfig()} onBusyChange={host.mobileGoodsBusy} onSaved={host.mobileGoodsSaved}/>);
