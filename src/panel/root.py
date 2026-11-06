"""Serve the compiled React control panel."""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(tags=["root"])
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "dist"


@router.get("/")
async def serve_control_panel():
    index = FRONTEND_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(503, "请先在 frontend 目录运行 npm install 和 npm run build")
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
