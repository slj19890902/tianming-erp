(function(global){
  "use strict";
  const methods={
    async toggleIncomingActual(row){
      if(row._actualOpen){row._actualOpen=false;return;}
      try{
        if(!row._actualCatalog){
          const {data}=await axios.get(`/api/incoming/actual-materials/${encodeURIComponent(row.item_id)}`);
          row._actualCatalog=data;
          row._actualDraft={material_id:Number(data.material_id)||Number(data.materials[0]?.id),length_mm:Number(data.length_mm),width_mm:Number(data.width_mm),supplier_edge_type:data.supplier_edge_type,sync_supplier_defaults:!!(data.product_version && this.hasPermission?.("products.edit"))};
        }
        row._actualOpen=true;
      }catch(error){this.showToast(this.errorMessage(error),true);}
    },
    incomingActualRequest(row){
      const draft=row._actualDraft;
      const material=row._actualCatalog?.materials.find(m=>Number(m.id)===Number(draft?.material_id));
      if(!draft||!material)throw new Error("请选择材质字典中的实际材质");
      const request={material_id:Number(material.id),material_version:Number(material.version),length_mm:Number(draft.length_mm),width_mm:Number(draft.width_mm)};
      if(draft.supplier_edge_type)request.supplier_edge_type=draft.supplier_edge_type;
      const original=row._actualCatalog;
      const changed=Number(draft.length_mm)!==Number(original.length_mm)||Number(draft.width_mm)!==Number(original.width_mm)||draft.supplier_edge_type!==original.supplier_edge_type;
      if(changed && draft.sync_supplier_defaults){request.sync_supplier_defaults=true;request.expected_product_version=original.product_version;}
      return request;
    },
    async previewIncomingActual(row){
      row._actualPreview=null;row._actualError="";
      const generation=(row._actualGeneration||0)+1;row._actualGeneration=generation;
      try{
        const request=this.incomingActualRequest(row);
        const {data}=await axios.post(`/api/incoming/actual-preview/${encodeURIComponent(row.item_id)}`,request);
        if(row._actualGeneration!==generation)return;
        row._actualPreview=data;row._actualRequest={...request,fingerprint:data.fingerprint};
      }catch(error){if(row._actualGeneration===generation){row._actualError=this.errorMessage(error);row._actualRequest=null;}}
    },
    async prepareIncomingActual(row){
      if(!row._actualDraft)return;
      await this.previewIncomingActual(row);
      if(!row._actualPreview)throw new Error(row._actualError||"请核对实际来料规格");
    },
    resetIncomingActual(row){
      row._actualGeneration=(row._actualGeneration||0)+1;
      row._actualDraft=null;row._actualPreview=null;row._actualRequest=null;row._actualCatalog=null;row._actualError="";row._actualOpen=false;
    },
    incomingActualSummary(row){
      const p=row._actualPreview;if(!p)return "";
      return `一开${p.cutting.cutting_factor} · ${p.cutting.mold_count}模 · 每张产${p.yield}片 · 本次${Number(row.incoming_quantity||0)*p.yield}片`+
        ((Number(p.trim_width_mm)||Number(p.trim_length_mm))?` · 余边${p.trim_length_mm}×${p.trim_width_mm}mm`:"");
    }
  };
  global.TianmingIncomingActual={methods};
})(typeof window!=="undefined"?window:globalThis);
