"""Administrator-only summaries and TXT downloads of marked system prompts."""
import io
import re
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from src.storage_adapter import get_storage_adapter
from src.utils import verify_panel_token

router = APIRouter(prefix="/marked-prompts", tags=["Marked prompts"], dependencies=[Depends(verify_panel_token)])
_NO_CACHE = {"Cache-Control": "no-store"}


class PromptDownload(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


async def download_records(ids):
    if any(not re.fullmatch(r"[0-9a-f]{64}", prompt_id) for prompt_id in ids):
        raise HTTPException(400, "提示词编号无效")
    storage = await get_storage_adapter()
    records = await storage.get_marked_prompts(list(dict.fromkeys(ids)))
    if len(records) != len(set(ids)):
        raise HTTPException(404, "部分提示词记录不存在，请刷新列表")
    return records


@router.get("")
async def list_prompts(offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
    storage = await get_storage_adapter()
    return JSONResponse(await storage.list_marked_prompts(offset, limit), headers=_NO_CACHE)


@router.get("/{prompt_id}/download")
async def download_prompt(prompt_id: str):
    record = (await download_records([prompt_id]))[0]
    return Response(record["text"].encode("utf-8-sig"), media_type="text/plain", headers={
        **_NO_CACHE, "Content-Disposition": f'attachment; filename="prompt-{prompt_id[:12]}.txt"',
    })


@router.post("/download")
async def download_prompts(request: PromptDownload):
    records = await download_records(request.ids)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for record in records:
            archive.writestr(f'prompt-{record["id"][:12]}.txt', record["text"].encode("utf-8-sig"))
    return Response(output.getvalue(), media_type="application/zip", headers={
        **_NO_CACHE, "Content-Disposition": 'attachment; filename="marked-prompts.zip"',
    })
