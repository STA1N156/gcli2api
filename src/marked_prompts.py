"""Shared, exact-match records of system prompts that triggered the marked-prompt error."""
import hashlib
import time

from log import log
from src.converter.anti_truncation import REPLY_TOOL_INSTRUCTION, move_system_to_first_user
from src.storage_adapter import get_storage_adapter


def system_prompt_identity(request):
    parts = (request.get("systemInstruction") or {}).get("parts") or []
    text = "\n\n".join(
        part["text"] for part in parts
        if isinstance(part.get("text"), str) and part["text"].strip() != REPLY_TOOL_INSTRUCTION
    )
    if not text.strip():
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), text


async def remember_marked_prompt(request, model):
    identity = system_prompt_identity(request)
    if not identity:
        return
    prompt_id, text = identity
    try:
        storage = await get_storage_adapter()
        await storage.save_marked_prompt({
            "id": prompt_id, "text": text, "model": model, "created_at": time.time(),
            "chars": len(text), "preview": " ".join(text[:180].split()),
        })
    except Exception as exc:
        log.warning(f"[标记提示词] 保存失败：{type(exc).__name__}")


async def relocate_known_prompt(body):
    identity = system_prompt_identity(body["request"])
    if not identity:
        return
    try:
        storage = await get_storage_adapter()
        if await storage.has_marked_prompt(identity[0]):
            body["request"] = move_system_to_first_user(body["request"])
            log.info(f"[标记提示词] 命中 {identity[0][:12]}，发送前直接搬移系统提示词")
    except Exception as exc:
        log.warning(f"[标记提示词] 查询失败，沿用正常请求：{type(exc).__name__}")
