# -*- coding: utf-8 -*-
"""
admin.py —— 管理后台（阶段 3：鉴权 + 知识库 + 提示词 + 对话审计）
================================================================
设计要点：
  1. 鉴权：登录签发 JWT（HS256），存 HttpOnly Cookie；每个管理接口用
     Depends(require_admin) 做接口级权限校验，普通员工访问返回 code=1003
  2. 服务端渲染：Jinja2 模板，类 SaaS 卡片风格，无前端框架
  3. 提示词存库，问答链路每次读库 → 改完立即生效
  4. 知识库：上传 md/pdf → 解析切片 → 向量化 → 重建索引
  5. 审计：按员工/时间/可疑标记筛选，可标记违规问答
"""

import os
import time
import json
import shutil
import pathlib
import datetime

import jwt
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.repositories import admin_db as adb
from app import rag

router = APIRouter(prefix="/admin")
from app.core.config import TEMPLATE_DIR, KB_UPLOAD_DIR, JWT_SECRET, JWT_ALG, JWT_EXPIRE_HOURS, COOKIE_NAME

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# JWT 配置（生产环境务必从环境变量注入密钥）

UPLOAD_DIR = pathlib.Path(KB_UPLOAD_DIR)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 错误码表（与 main.py 保持同一套语义）
ERRORS = {
    "FORBIDDEN":         (1003, "无权限访问该功能"),
    "LOGIN_FAILED":      (1004, "账号或密码错误"),
    "TOKEN_EXPIRED":     (1005, "登录已过期，请重新登录"),
    "UPLOAD_TYPE_INVALID": (5001, "仅支持 .md 与 .pdf 文件"),
    "UPLOAD_PARSE_FAILED": (5002, "文件解析失败，请检查内容格式"),
    "PROMPT_TOO_LONG":   (5003, "提示词过长，请控制在 4000 字以内"),
    "INTERNAL":          (9999, "系统内部错误，请稍后重试"),
}


def err(key: str, extra: str = "") -> JSONResponse:
    code, msg = ERRORS.get(key, ERRORS["INTERNAL"])
    return JSONResponse({"code": code, "message": f"{msg}{('：' + extra) if extra else ''}",
                         "data": None})


def ok(data=None, message="成功"):
    return JSONResponse({"code": 0, "message": message, "data": data})


# ============================================================
# 一、JWT 鉴权
# ============================================================
def create_token(user) -> str:
    """签发 JWT：sub=用户ID，role 用于接口级权限判断。"""
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str):
    """解析 JWT，失败返回 None（区分过期与非法）。"""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        return "expired"
    except jwt.InvalidTokenError:
        return None


def current_user(request: Request):
    """从 Cookie 或 Authorization 头取当前登录用户，返回 (payload, 错误)。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None, "TOKEN_EXPIRED"
    payload = decode_token(token)
    if payload == "expired":
        return None, "TOKEN_EXPIRED"
    if not payload:
        return None, "TOKEN_EXPIRED"
    return payload, None


def require_admin(request: Request):
    """接口级权限校验依赖：必须是已登录且 role=admin 的用户。
    未登录抛 401 语义（返回 code=1005），员工越权返回 code=1003。"""
    payload, error = current_user(request)
    if error:
        raise PermissionError("TOKEN_EXPIRED")
    if payload.get("role") != "admin":
        raise PermissionError("FORBIDDEN")
    return payload


def require_login(request: Request):
    """普通登录态校验（审计/概览也要求管理员，这里统一用 require_admin）。"""
    return require_admin(request)


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/api/login")
def api_login(body: LoginIn):
    user = adb.get_user_by_username(body.username.strip())
    if not user or not adb.verify_password(body.password, user["password_hash"]):
        return err("LOGIN_FAILED")
    token = create_token(user)
    resp = ok({"role": user["role"], "name": user["name"], "username": user["username"]})
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                    max_age=JWT_EXPIRE_HOURS * 3600)   # HttpOnly 防 XSS 窃取
    return resp


@router.post("/api/logout")
def api_logout():
    resp = ok(None, "已退出登录")
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/api/me")
def api_me(request: Request):
    payload, error = current_user(request)
    if error:
        return err(error)
    return ok({"user_id": payload["sub"], "name": payload["name"],
               "username": payload["username"], "role": payload["role"]})


# ============================================================
# 二、页面路由（服务端渲染）
# ============================================================
def guard_page(request: Request):
    """页面守卫：未登录或非管理员 → 跳转登录页。"""
    payload, error = current_user(request)
    if error or payload.get("role") != "admin":
        return None
    return payload


@router.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request):
    user = guard_page(request)
    if not user:
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user, "active": "dashboard",
        "stats": adb.stats()})


@router.get("/kb", response_class=HTMLResponse)
def page_kb(request: Request):
    user = guard_page(request)
    if not user:
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse("kb.html", {
        "request": request, "user": user, "active": "kb",
        "docs": adb.list_documents()})


@router.get("/prompts", response_class=HTMLResponse)
def page_prompts(request: Request):
    user = guard_page(request)
    if not user:
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse("prompts.html", {
        "request": request, "user": user, "active": "prompts",
        "current": adb.get_active_prompt(),
        "history": adb.list_prompts(10),
        "default_prompt": adb.DEFAULT_PROMPT})


@router.get("/audit", response_class=HTMLResponse)
def page_audit(request: Request, user_id: str = "", start: str = "", end: str = "",
               flagged: str = "", page: int = 1):
    user = guard_page(request)
    if not user:
        return RedirectResponse("/admin/login")
    flagged_flag = True if flagged == "1" else None
    rows, total = adb.list_conversations(
        user_id=user_id or None, start=start or None, end=end or None,
        flagged=flagged_flag, page=page, size=20)
    return templates.TemplateResponse("audit.html", {
        "request": request, "user": user, "active": "audit",
        "rows": rows, "total": total, "page": page,
        "filters": {"user_id": user_id, "start": start, "end": end, "flagged": flagged}})


# ============================================================
# 三、知识库管理 API
# ============================================================
@router.get("/api/kb/documents")
def api_kb_list(user=Depends(require_admin)):
    return ok(adb.list_documents())


@router.post("/api/kb/upload")
async def api_kb_upload(file: UploadFile = File(...),
                        category: str = Form("就诊须知"),
                        title: str = Form(""),
                        user=Depends(require_admin)):
    """上传 → 落盘 → 解析切片 → 向量化入库 → 重建索引。"""
    filename = file.filename or "unnamed.md"
    ext = pathlib.Path(filename).suffix.lower()
    if ext not in (".md", ".markdown", ".pdf"):
        return err("UPLOAD_TYPE_INVALID", f"收到 {ext or '无扩展名'} 文件")

    # 文件名加时间戳前缀，避免同名覆盖
    safe_name = f"{int(time.time())}_{pathlib.Path(filename).name}"
    dest = UPLOAD_DIR / safe_name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        # 先解析并切片，确认文件可读再入库
        text = rag.load_document(str(dest))
        chunks = rag.split_chunks(text)
        if not chunks:
            dest.unlink(missing_ok=True)
            return err("UPLOAD_PARSE_FAILED", "文档内容为空或无法提取文字")
        doc_title = title.strip() or pathlib.Path(filename).stem
        doc_id = adb.add_document(doc_title, filename, ext.lstrip("."),
                                  len(chunks), user["username"], category, str(dest))
        # 重建索引（含新文档），失败则回滚元数据
        try:
            total = rag.rebuild()
        except Exception:
            adb.delete_document(doc_id)
            dest.unlink(missing_ok=True)
            # 不暴露模型服务底层报错原文，仅提示排查方向
            return err("UPLOAD_PARSE_FAILED", "向量化服务不可用，请检查模型 API 配置后重试")
        return ok({"doc_id": doc_id, "chunk_count": len(chunks), "index_total": total},
                  "上传成功，索引已更新")
    except Exception as e:
        dest.unlink(missing_ok=True)
        return err("UPLOAD_PARSE_FAILED", "文件解析失败，请检查内容是否为可提取文本")


@router.delete("/api/kb/documents/{doc_id}")
def api_kb_delete(doc_id: int, user=Depends(require_admin)):
    if not adb.delete_document(doc_id):
        return JSONResponse({"code": 5004, "message": "文档不存在或已删除", "data": None})
    rag.rebuild()          # 删除后重建索引，确保检索不再命中
    return ok(None, "已删除并更新索引")


@router.post("/api/kb/rebuild")
def api_kb_rebuild(user=Depends(require_admin)):
    try:
        return ok({"chunks": rag.rebuild()}, "索引重建完成")
    except rag.RagError as e:
        return JSONResponse({"code": 4001, "message": e.message, "data": None})


# ============================================================
# 四、提示词模板 API
# ============================================================
class PromptIn(BaseModel):
    content: str


@router.get("/api/prompt")
def api_prompt_get(user=Depends(require_admin)):
    return ok({"current": adb.get_active_prompt(), "history": adb.list_prompts(10)})


@router.put("/api/prompt")
def api_prompt_save(body: PromptIn, user=Depends(require_admin)):
    content = body.content.strip()
    if not content:
        return err("PROMPT_TOO_LONG", "提示词不能为空")
    if len(content) > 4000:
        return err("PROMPT_TOO_LONG")
    version = adb.save_prompt(content, user["username"])
    # 无需重启：rag.get_system_prompt() 每次问答实时读库
    return ok({"version": version}, f"已保存（v{version}），问答立即生效")


@router.post("/api/prompt/reset")
def api_prompt_reset(user=Depends(require_admin)):
    version = adb.save_prompt(adb.DEFAULT_PROMPT, user["username"])
    return ok({"version": version}, "已恢复默认模板并立即生效")


@router.post("/api/prompt/rollback/{version}")
def api_prompt_rollback(version: int, user=Depends(require_admin)):
    if not adb.rollback_prompt(version, user["username"]):
        return JSONResponse({"code": 5005, "message": "该版本不存在", "data": None})
    return ok(None, f"已回滚到 v{version}")


# ============================================================
# 五、对话审计 API
# ============================================================
@router.get("/api/audit/conversations")
def api_audit_list(user_id: str = "", start: str = "", end: str = "",
                   flagged: str = "", page: int = 1, size: int = 20,
                   user=Depends(require_admin)):
    rows, total = adb.list_conversations(
        user_id=user_id or None, start=start or None, end=end or None,
        flagged=True if flagged == "1" else None, page=page, size=size)
    return ok({"items": rows, "total": total, "page": page, "size": size})


@router.get("/api/audit/messages/{conv_id}")
def api_audit_messages(conv_id: str, user=Depends(require_admin)):
    return ok(adb.list_messages(conv_id))


class FlagIn(BaseModel):
    message_id: int
    flagged: bool
    reason: str = ""


@router.post("/api/audit/flag")
def api_audit_flag(body: FlagIn, user=Depends(require_admin)):
    if not adb.flag_message(body.message_id, body.flagged, body.reason, user["username"]):
        return JSONResponse({"code": 5006, "message": "消息不存在", "data": None})
    return ok(None, "已标记为疑似违规" if body.flagged else "已取消标记")
