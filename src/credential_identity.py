"""导入按邮箱识别账号，内部文件名继续兼容已有接口和会话绑定。"""

import hashlib
import re


def normalize_email(value) -> str:
    if not isinstance(value, str):
        return ""
    email = value.strip().lower()
    return email if len(email) <= 320 and re.fullmatch(r"[^@\s]+@[^@\s]+", email) else ""


def email_filename(email: str) -> str:
    return f"email_{hashlib.sha256(email.encode()).hexdigest()}.json"
