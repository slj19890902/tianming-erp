from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
from app.models.tianhua_pre_delivery import TianhuaPreDeliveryImportBatch
from app.models.user import User
from app.services.tianhua_pre_delivery import batch_dict, create_batch, draft_dict, save_draft

router=APIRouter()
can_read=RoleChecker(["admin","finance","sales","workshop"])
can_operate=RoleChecker(["admin","sales"])


class DraftLine(BaseModel):
    item_id:int|None=Field(default=None,ge=1)
    row_no:int=Field(ge=1)
    selected:bool=False
    final_delivery_qty:int|None=Field(default=None,ge=1)


class DraftRequest(BaseModel):
    items:list[DraftLine]
    remark:str|None=None


def _batch(db,batch_id):
    value=db.get(TianhuaPreDeliveryImportBatch,batch_id)
    if value is None: raise HTTPException(404,"天华预送货识别批次不存在")
    return value


@router.post("/tianhua-preimport/upload",status_code=status.HTTP_201_CREATED)
async def upload(file:UploadFile=File(...),db:Session=Depends(get_db),user:User=Depends(can_operate)):
    filename=(file.filename or "").strip()
    if not any(filename.lower().endswith(s) for s in (".jpg",".jpeg",".png")): raise HTTPException(400,"仅支持 jpg、jpeg、png 图片")
    content=await file.read(12*1024*1024+1)
    if not content: raise HTTPException(400,"上传图片为空")
    if len(content)>12*1024*1024: raise HTTPException(413,"图片不能超过 12MB")
    try: return batch_dict(db,create_batch(db,content,filename,user.id))
    except ValueError as e: db.rollback(); raise HTTPException(400,str(e)) from e


@router.get("/tianhua-preimport/{batch_id}")
def get_batch(batch_id:int,db:Session=Depends(get_db),_user:User=Depends(can_read)):
    return batch_dict(db,_batch(db,batch_id))


def _save(batch_id,payload,db,user,update):
    try:
        draft=save_draft(db,_batch(db,batch_id),[x.model_dump() for x in payload.items],payload.remark,user.id,update)
        return draft_dict(db,draft)
    except RuntimeError as e: db.rollback(); raise HTTPException(409,str(e)) from e
    except ValueError as e: db.rollback(); raise HTTPException(400,str(e)) from e


@router.post("/tianhua-preimport/{batch_id}/create-draft",status_code=status.HTTP_201_CREATED)
def create_draft(batch_id:int,payload:DraftRequest,db:Session=Depends(get_db),user:User=Depends(can_operate)):
    return _save(batch_id,payload,db,user,False)


@router.put("/tianhua-preimport/{batch_id}/update-draft")
def update_draft(batch_id:int,payload:DraftRequest,db:Session=Depends(get_db),user:User=Depends(can_operate)):
    return _save(batch_id,payload,db,user,True)
