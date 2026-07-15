"""Telegram Image Gallery — deterministic image intake for Crispal.

Moves product-photo intake out of the LLM context: an admin mints a
share-link token via `/images` in Telegram, uploads a batch through this
webapp, and the bot later calls the `list_gallery_images` MCP tool to get
flat file paths for `arvin`/`crispal_image_search` — no image bytes ever
enter the model's context window.

Auth mirrors awserv's presentation share-token convention: a query-param
token validated on every route (exists, not revoked, not expired). No
session/cookie state — the token itself is the credential.
"""
from __future__ import annotations

import mimetypes
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from ..core.security import get_setting
from ..db import session_scope
from ..models import GalleryBlock, GalleryImage, GalleryImageTag, GalleryTag, GalleryToken, _normalize_tag

router = APIRouter(tags=["gallery"])

GALLERY_DIR = Path("/opt/agentic-workspace/data/tmp/gallery")
RETENTION_DAYS = 90
DEFAULT_NOTIFY_CHAT_ID = "-5376867602"  # "Crispal - Imagens" Telegram group

_last_prune_ts = 0.0
_PRUNE_INTERVAL_S = 3600  # at most once an hour, triggered by real upload traffic


def _get_valid_token(s, token: str) -> GalleryToken:
    row = s.get(GalleryToken, token)
    if not row or row.revoked or row.expires_at < datetime.utcnow():
        raise HTTPException(401, "invalid, expired, or revoked token")
    return row


def _maybe_prune_old_blocks() -> None:
    """Fire-and-forget, best-effort: delete blocks (+ on-disk files) older
    than RETENTION_DAYS, at most once per _PRUNE_INTERVAL_S. Never raises —
    a failure here must not break an upload."""
    global _last_prune_ts
    now = time.time()
    if now - _last_prune_ts < _PRUNE_INTERVAL_S:
        return
    _last_prune_ts = now
    try:
        cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
        with session_scope() as s:
            stale = s.query(GalleryBlock).filter(GalleryBlock.created_at < cutoff).all()
            for block in stale:
                block_dir = GALLERY_DIR / block.id
                for img in block.images:
                    try:
                        Path(img.file_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                try:
                    block_dir.rmdir()
                except Exception:
                    pass
                s.delete(block)
    except Exception:
        pass


@router.get("/gallery", response_class=HTMLResponse)
def gallery_page():
    return HTMLResponse(_GALLERY_HTML)


@router.post("/api/gallery/{token}/upload")
async def upload_images(token: str, files: list[UploadFile]):
    if not files:
        raise HTTPException(400, "no files provided")
    _maybe_prune_old_blocks()
    with session_scope() as s:
        tok = _get_valid_token(s, token)
        block = GalleryBlock(
            token=token, bot_slug=tok.bot_slug, origin_chat_id=tok.origin_chat_id,
            image_count=len(files),
        )
        s.add(block)
        s.flush()  # assigns block.id

        block_dir = GALLERY_DIR / block.id
        block_dir.mkdir(parents=True, exist_ok=True)

        images_out = []
        for f in files:
            ext = Path(f.filename or "").suffix or mimetypes.guess_extension(f.content_type or "") or ""
            image_id = uuid.uuid4().hex
            dest = block_dir / f"{image_id}{ext}"
            data = await f.read()
            dest.write_bytes(data)
            img = GalleryImage(
                id=image_id, block_id=block.id, file_path=str(dest),
                original_name=f.filename or "", mime=f.content_type or "", bytes=len(data),
            )
            s.add(img)
            images_out.append({"id": image_id, "file_path": str(dest), "original_name": f.filename or ""})

        block.notified_at = datetime.utcnow()
        block_id, image_count = block.id, block.image_count
        origin_chat_id, bot_slug = block.origin_chat_id, block.bot_slug

    _notify_new_block(bot_slug, origin_chat_id, block_id, image_count)
    return {"block_id": block_id, "image_count": image_count, "images": images_out}


@router.get("/api/gallery/{token}/blocks")
def list_blocks(token: str):
    with session_scope() as s:
        tok = _get_valid_token(s, token)
        blocks = (s.query(GalleryBlock)
                  .filter(GalleryBlock.bot_slug == tok.bot_slug)
                  .order_by(GalleryBlock.created_at.desc())
                  .all())

        image_ids = [img.id for b in blocks for img in b.images]
        tags_by_image = _load_tags_by_image(s, image_ids)

        return {"blocks": [
            {
                "id": b.id, "image_count": b.image_count, "created_at": b.created_at.isoformat(),
                "images": [
                    {"id": img.id, "tags": tags_by_image.get(img.id, [])}
                    for img in b.images
                ],
            }
            for b in blocks
        ]}


def _load_tags_by_image(s, image_ids: list[str]) -> dict[str, list[dict]]:
    """One aggregate query for every image on the page — avoids per-image
    N+1 lookups for the grid's 🏷 badge / lightbox tag chips."""
    if not image_ids:
        return {}
    rows = (
        s.query(GalleryImageTag.image_id, GalleryTag.id, GalleryTag.name)
        .join(GalleryTag, GalleryTag.id == GalleryImageTag.tag_id)
        .filter(GalleryImageTag.image_id.in_(image_ids))
        .all()
    )
    out: dict[str, list[dict]] = {}
    for image_id, tag_id, name in rows:
        out.setdefault(image_id, []).append({"id": tag_id, "name": name})
    return out


@router.get("/api/gallery/{token}/image/{image_id}")
def get_image(token: str, image_id: str):
    with session_scope() as s:
        _get_valid_token(s, token)
        img = s.get(GalleryImage, image_id)
        if not img or not Path(img.file_path).is_file():
            raise HTTPException(404, "image not found")
        data = Path(img.file_path).read_bytes()
        return Response(content=data, media_type=img.mime or "application/octet-stream")


@router.delete("/api/gallery/{token}/image/{image_id}")
def delete_image(token: str, image_id: str):
    with session_scope() as s:
        tok = _get_valid_token(s, token)
        img = s.get(GalleryImage, image_id)
        if not img:
            raise HTTPException(404, "image not found")
        block = s.get(GalleryBlock, img.block_id)
        if not block or block.bot_slug != tok.bot_slug:
            raise HTTPException(404, "image not found")

        Path(img.file_path).unlink(missing_ok=True)
        s.delete(img)
        block.image_count = max(0, block.image_count - 1)
        block_deleted = block.image_count == 0
        block_id = block.id
        if block_deleted:
            s.delete(block)

        image_count = block.image_count

    if block_deleted:
        try:
            (GALLERY_DIR / block_id).rmdir()
        except Exception:
            pass

    return {"ok": True, "block_id": block_id, "block_deleted": block_deleted, "image_count": image_count}


@router.get("/api/gallery/{token}/tags")
def list_tags(token: str, q: str = "", limit: int = 20):
    with session_scope() as s:
        tok = _get_valid_token(s, token)
        qn = _normalize_tag(q)

        rows = (
            s.query(GalleryTag.id, GalleryTag.name, GalleryImageTag.image_id)
            .outerjoin(GalleryImageTag, GalleryImageTag.tag_id == GalleryTag.id)
            .filter(GalleryTag.bot_slug == tok.bot_slug)
            .all()
        )
        counts: dict[str, int] = {}
        names: dict[str, str] = {}
        for tag_id, name, image_id in rows:
            names[tag_id] = name
            counts[tag_id] = counts.get(tag_id, 0) + (1 if image_id else 0)

        matches = [
            {"id": tag_id, "name": names[tag_id], "image_count": counts[tag_id]}
            for tag_id in names
            if not qn or qn in _normalize_tag(names[tag_id])
        ]
        matches.sort(key=lambda t: (-t["image_count"], t["name"].lower()))
        matches = matches[:limit]

        exact_match = any(_normalize_tag(t["name"]) == qn for t in matches) if qn else False
        return {"tags": matches, "exact_match": exact_match}


class _TagBody(BaseModel):
    name: str


@router.post("/api/gallery/{token}/image/{image_id}/tag")
def apply_tag(token: str, image_id: str, body: _TagBody):
    with session_scope() as s:
        tok = _get_valid_token(s, token)
        img = s.get(GalleryImage, image_id)
        if not img:
            raise HTTPException(404, "image not found")

        normalized = _normalize_tag(body.name)
        if not normalized:
            raise HTTPException(400, "tag name cannot be empty")

        tag = (
            s.query(GalleryTag)
            .filter(GalleryTag.bot_slug == tok.bot_slug, GalleryTag.normalized_name == normalized)
            .first()
        )
        created = False
        if not tag:
            tag = GalleryTag(bot_slug=tok.bot_slug, name=body.name.strip(), normalized_name=normalized)
            s.add(tag)
            try:
                s.flush()
                created = True
            except IntegrityError:
                s.rollback()
                tag = (
                    s.query(GalleryTag)
                    .filter(GalleryTag.bot_slug == tok.bot_slug, GalleryTag.normalized_name == normalized)
                    .first()
                )

        already_applied = (
            s.query(GalleryImageTag)
            .filter(GalleryImageTag.image_id == image_id, GalleryImageTag.tag_id == tag.id)
            .first()
            is not None
        )
        if not already_applied:
            s.add(GalleryImageTag(image_id=image_id, tag_id=tag.id))

        return {"tag": {"id": tag.id, "name": tag.name}, "created": created, "already_applied": already_applied}


@router.delete("/api/gallery/{token}/image/{image_id}/tag/{tag_id}")
def remove_tag(token: str, image_id: str, tag_id: str):
    with session_scope() as s:
        _get_valid_token(s, token)
        link = (
            s.query(GalleryImageTag)
            .filter(GalleryImageTag.image_id == image_id, GalleryImageTag.tag_id == tag_id)
            .first()
        )
        if link:
            s.delete(link)
            s.flush()

        remaining = (
            s.query(GalleryImageTag).filter(GalleryImageTag.tag_id == tag_id).count()
        )
        tag_gc = False
        if remaining == 0:
            tag = s.get(GalleryTag, tag_id)
            if tag:
                s.delete(tag)
                tag_gc = True

        return {"ok": True, "tag_gc": tag_gc}


def _notify_new_block(bot_slug: str, origin_chat_id: str, block_id: str, image_count: int) -> None:
    """Ping the Crispal images group (default target — not the `/images`
    invocation chat, per Product Owner's Q7 resolution: that group is the
    only channel real photo-batch work happens in). Configurable per-bot
    override via Setting "gallery_notify_chat_id:<bot_slug>"."""
    from .telegram import _send_message
    from ..models import TelegramBot

    try:
        with session_scope() as s:
            bot = s.query(TelegramBot).filter(TelegramBot.id == bot_slug).first()
            if not bot:
                return
            target_chat_id = get_setting(f"gallery_notify_chat_id:{bot_slug}", DEFAULT_NOTIFY_CHAT_ID)
            _send_message(bot.token, target_chat_id,
                          f"📸 {image_count} novas imagens na galeria, bloco {block_id}.")
    except Exception:
        pass


_GALLERY_HTML = r"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Galeria de Imagens — Crispal</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0d0d0f;--surface:#1a1a1f;--border:#2a2a32;--fg:#e8e8ed;--hint:#8e8e98;--blue:#0a84ff;--green:#30d158;--red:#ff453a}
html,body{height:100%}
body{background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:flex;flex-direction:column;padding:14px;gap:14px;padding-bottom:32px}
#head{font-size:13px;color:var(--hint)}
#head b{color:var(--fg);font-size:16px;display:block;margin-bottom:2px}
#addBtn{background:var(--surface);color:var(--fg);border:1px solid var(--border);border-radius:10px;padding:12px;font-size:15px;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px}
#addBtn:active{background:var(--border)}
#save{display:none;background:var(--green);color:#000;border:none;border-radius:10px;padding:12px;font-size:15px;font-weight:600;cursor:pointer}
#save:disabled{background:var(--border);color:var(--hint);cursor:not-allowed}
#status{font-size:13px;padding:2px 0;min-height:16px}
#status.ok{color:var(--green)}
#status.err{color:var(--red)}
#status.err a{color:var(--blue)}
#stagedGrid,.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.thumb{position:relative;aspect-ratio:1;border-radius:8px;overflow:hidden;background:var(--surface);border:1px solid var(--border)}
.thumb img{width:100%;height:100%;object-fit:cover;display:block;cursor:pointer}
.thumb .rm{position:absolute;top:4px;right:4px;width:20px;height:20px;border-radius:50%;background:rgba(0,0,0,.65);color:#fff;border:none;font-size:13px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center}
.thumb .fname{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(transparent,rgba(0,0,0,.75));color:#fff;font-size:9px;padding:10px 4px 3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.skel{aspect-ratio:1;border-radius:8px;background:linear-gradient(90deg,var(--surface) 25%,var(--border) 50%,var(--surface) 75%);background-size:200% 100%;animation:shimmer 1.4s infinite}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
.progressWrap{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 12px;display:none;flex-direction:column;gap:6px}
.progressBar{height:6px;border-radius:3px;background:var(--border);overflow:hidden}
.progressFill{height:100%;background:var(--blue);width:0%;transition:width .15s}
.block{display:flex;flex-direction:column;gap:8px}
.block .bhead{display:flex;align-items:baseline;gap:8px;font-size:13px;color:var(--hint)}
.block .bhead .time{color:var(--fg);font-weight:600}
.block .bhead .pill{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:1px 8px;font-size:11px}
#empty{display:none;flex-direction:column;align-items:center;text-align:center;gap:8px;padding:48px 16px;color:var(--hint)}
#empty .icon{font-size:40px}
#lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:50;flex-direction:column}
#lightbox .lbTop{display:flex;justify-content:flex-end;padding:12px;gap:10px}
#lightbox .lbTop button{width:36px;height:36px;border-radius:50%;background:rgba(255,255,255,.1);border:none;color:#fff;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center}
#lightbox #lbDelete{color:var(--red)}
#lightbox .lbBody{flex:1;display:flex;align-items:center;justify-content:center;min-height:0}
#lightbox .lbBody img{max-width:94vw;max-height:70vh;object-fit:contain}
#lightbox .lbTags{padding:8px 16px 20px;display:flex;flex-direction:column;gap:8px}
#lightbox .lbTagsLabel{font-size:12px;color:var(--hint)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:5px 8px 5px 12px;font-size:13px;display:flex;align-items:center;gap:6px}
.chip button{background:none;border:none;color:var(--hint);font-size:13px;cursor:pointer;line-height:1}
.chip.add{background:none;border:1px dashed var(--blue);color:var(--blue);cursor:pointer;padding:5px 12px}
.tagAuto{position:relative}
.tagAuto .tagInputRow{display:flex;gap:6px;align-items:center}
.tagAuto input{flex:1;width:auto;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 12px;color:var(--fg);font-size:14px}
.tagAuto .tagDoneBtn{flex-shrink:0;width:36px;height:36px;border-radius:10px;background:var(--blue);color:#fff;border:none;font-size:16px;font-weight:600;cursor:pointer}
.tagAuto .drop{position:absolute;left:0;right:0;top:100%;margin-top:4px;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;z-index:5;max-height:180px;overflow-y:auto}
.tagAuto.kbFixed{position:fixed;left:16px;right:16px;z-index:60}
.tagAuto.kbFixed .drop{top:auto;bottom:100%;margin-top:0;margin-bottom:4px}
.tagAuto .drop div{padding:10px 12px;font-size:14px;cursor:pointer}
.tagAuto .drop div:active{background:var(--border)}
.tagAuto .drop .create{color:var(--blue)}
#confirmModal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:60;align-items:flex-end;justify-content:center}
#confirmModal .card{background:var(--surface);border:1px solid var(--border);border-radius:14px 14px 0 0;padding:20px;width:100%;max-width:480px;display:flex;flex-direction:column;gap:14px}
#confirmModal .msg{font-size:15px}
#confirmModal .row{display:flex;gap:10px}
#confirmModal button{flex:1;border:none;border-radius:10px;padding:12px;font-size:15px;font-weight:600;cursor:pointer}
#confirmModal .cancel{background:var(--border);color:var(--fg)}
#confirmModal .danger{background:var(--red);color:#fff}
.thumb .tagBadge{position:absolute;top:4px;left:4px;background:rgba(0,0,0,.65);border-radius:50%;width:20px;height:20px;display:flex;align-items:center;justify-content:center;font-size:11px}
</style>
</head>
<body>
<div id="head"><b>Galeria de Imagens</b>Fotos enviadas nas últimas conversas com a Crispal</div>

<input type="file" id="fileInput" multiple accept="image/*" style="display:none">
<button id="addBtn">➕ Adicionar fotos</button>

<div id="stagedGrid"></div>
<div class="progressWrap" id="progressWrap">
  <div class="progressBar"><div class="progressFill" id="progressFill"></div></div>
  <div id="progressLabel" style="font-size:12px;color:var(--hint)"></div>
</div>
<div id="status"></div>
<button id="save">Enviar</button>

<div id="empty"><div class="icon">🖼️</div><div>Nenhuma imagem ainda.<br>Toque em "Adicionar fotos" para começar.</div></div>
<div id="gallery"></div>

<div id="lightbox">
  <div class="lbTop"><button id="lbDelete">🗑</button></div>
  <div class="lbTags">
    <div class="lbTagsLabel">Tags</div>
    <div class="chips" id="lbChips">
      <div class="chip add" id="lbAddTagBtn">+ Tag</div>
    </div>
    <div class="tagAuto" id="tagAuto"></div>
  </div>
  <div class="lbBody"><img id="lightboxImg" src=""></div>
</div>

<div id="confirmModal">
  <div class="card">
    <div class="msg">Apagar esta imagem? Esta ação não pode ser desfeita.</div>
    <div class="row">
      <button class="cancel" id="confirmCancel">Cancelar</button>
      <button class="danger" id="confirmDelete">Apagar</button>
    </div>
  </div>
</div>

<script>
const params = new URLSearchParams(location.search);
const token = params.get("token") || "";
const tg = window.Telegram && window.Telegram.WebApp;

const addBtn = document.getElementById("addBtn");
const fileInput = document.getElementById("fileInput");
const stagedGrid = document.getElementById("stagedGrid");
const saveBtn = document.getElementById("save");
const statusEl = document.getElementById("status");
const galleryEl = document.getElementById("gallery");
const emptyEl = document.getElementById("empty");
const progressWrap = document.getElementById("progressWrap");
const progressFill = document.getElementById("progressFill");
const progressLabel = document.getElementById("progressLabel");
const lightbox = document.getElementById("lightbox");
const lightboxImg = document.getElementById("lightboxImg");
const lbDelete = document.getElementById("lbDelete");
const lbChips = document.getElementById("lbChips");
const tagAuto = document.getElementById("tagAuto");
const lbAddTagBtn = document.getElementById("lbAddTagBtn");
const confirmModal = document.getElementById("confirmModal");
const confirmCancel = document.getElementById("confirmCancel");
const confirmDelete = document.getElementById("confirmDelete");

let staged = []; // [{file, url}]
let currentImageId = null;
let currentTags = []; // [{id, name}] for the open lightbox image

function setStatus(text, cls) {
  statusEl.textContent = text || "";
  statusEl.className = cls || "";
}

function fmtTime(iso) {
  const d = new Date(iso + "Z");
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  const isYest = d.toDateString() === yest.toDateString();
  const hm = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  if (sameDay) return `Hoje, ${hm}`;
  if (isYest) return `Ontem, ${hm}`;
  const dm = d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });
  return `${dm}, ${hm}`;
}

function renderStaged() {
  stagedGrid.innerHTML = "";
  staged.forEach((s, i) => {
    const t = document.createElement("div");
    t.className = "thumb";
    t.innerHTML = `<img src="${s.url}"><button class="rm" data-i="${i}">×</button><div class="fname">${s.file.name}</div>`;
    stagedGrid.appendChild(t);
  });
  stagedGrid.querySelectorAll(".rm").forEach(btn => {
    btn.addEventListener("click", () => {
      const i = parseInt(btn.dataset.i, 10);
      URL.revokeObjectURL(staged[i].url);
      staged.splice(i, 1);
      renderStaged();
      updateConfirmButton();
    });
  });
}

function updateConfirmButton() {
  const n = staged.length;
  saveBtn.style.display = n ? "block" : "none";
  saveBtn.textContent = `Enviar ${n} foto${n === 1 ? "" : "s"}`;
  if (tg && tg.MainButton) {
    if (n) {
      tg.MainButton.setText(`Enviar ${n} foto${n === 1 ? "" : "s"}`);
      tg.MainButton.show();
    } else {
      tg.MainButton.hide();
    }
  }
}

addBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", () => {
  for (const f of fileInput.files) {
    staged.push({ file: f, url: URL.createObjectURL(f) });
  }
  fileInput.value = "";
  renderStaged();
  updateConfirmButton();
});

function renderSkeleton() {
  galleryEl.innerHTML = "";
  emptyEl.style.display = "none";
  for (let b = 0; b < 2; b++) {
    const grid = document.createElement("div");
    grid.className = "grid";
    for (let i = 0; i < 3; i++) {
      const sk = document.createElement("div");
      sk.className = "skel";
      grid.appendChild(sk);
    }
    galleryEl.appendChild(grid);
  }
}

function renderBlocks(blocks) {
  galleryEl.innerHTML = "";
  if (!blocks.length) {
    emptyEl.style.display = "flex";
    return;
  }
  emptyEl.style.display = "none";
  for (const b of blocks) {
    galleryEl.appendChild(renderBlock(b));
  }
}

function renderBlock(b) {
  const div = document.createElement("div");
  div.className = "block";
  const head = document.createElement("div");
  head.className = "bhead";
  head.innerHTML = `<span class="time">${fmtTime(b.created_at)}</span><span class="pill">${b.image_count} foto${b.image_count === 1 ? "" : "s"}</span>`;
  div.appendChild(head);
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const img of (b.images || [])) {
    const t = document.createElement("div");
    t.className = "thumb";
    t.dataset.imgId = img.id;
    const el = document.createElement("img");
    el.loading = "lazy";
    el.src = `/api/gallery/${token}/image/${img.id}`;
    el.addEventListener("click", () => openLightbox(img.id, el.src, img.tags || []));
    t.appendChild(el);
    if ((img.tags || []).length) {
      const badge = document.createElement("div");
      badge.className = "tagBadge";
      badge.textContent = "🏷";
      t.appendChild(badge);
    }
    grid.appendChild(t);
  }
  div.appendChild(grid);
  return div;
}

function thumbBadge(imageId) {
  return galleryEl.querySelector(`.thumb[data-img-id="${imageId}"]`);
}

function setThumbTagged(imageId, tagged) {
  const t = thumbBadge(imageId);
  if (!t) return;
  let badge = t.querySelector(".tagBadge");
  if (tagged && !badge) {
    badge = document.createElement("div");
    badge.className = "tagBadge";
    badge.textContent = "🏷";
    t.appendChild(badge);
  } else if (!tagged && badge) {
    badge.remove();
  }
}

function openLightbox(imageId, src, tags) {
  currentImageId = imageId;
  currentTags = tags.slice();
  lightboxImg.src = src;
  renderChips();
  closeTagDropdown();
  lightbox.style.display = "flex";
  if (tg && tg.BackButton) {
    tg.BackButton.show();
    tg.BackButton.onClick(closeLightbox);
  }
}

function closeLightbox() {
  lightbox.style.display = "none";
  lightboxImg.src = "";
  currentImageId = null;
  currentTags = [];
  closeTagDropdown();
  if (tg && tg.BackButton) tg.BackButton.hide();
}

function renderChips() {
  lbChips.innerHTML = "";
  for (const tag of currentTags) {
    const chip = document.createElement("div");
    chip.className = "chip";
    chip.innerHTML = `<span>${tag.name}</span><button data-tag-id="${tag.id}">×</button>`;
    chip.querySelector("button").addEventListener("click", () => removeTag(tag.id));
    lbChips.appendChild(chip);
  }
  lbChips.appendChild(lbAddTagBtn);
}

async function removeTag(tagId) {
  if (!currentImageId) return;
  try {
    await fetch(`/api/gallery/${token}/image/${currentImageId}/tag/${tagId}`, { method: "DELETE" });
    currentTags = currentTags.filter(t => t.id !== tagId);
    renderChips();
    setThumbTagged(currentImageId, currentTags.length > 0);
  } catch (e) {}
}

async function applyTag(name, opts) {
  opts = opts || {};
  if (!currentImageId || !name.trim()) return;
  try {
    const res = await fetch(`/api/gallery/${token}/image/${currentImageId}/tag`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!res.ok) return;
    const data = await res.json();
    if (!currentTags.some(t => t.id === data.tag.id)) {
      currentTags.push(data.tag);
      renderChips();
      setThumbTagged(currentImageId, true);
    }
  } catch (e) {}
  if (!opts.keepOpen) closeTagDropdown();
}

function closeTagDropdown() {
  const existing = tagAuto.querySelector(".drop");
  if (existing) existing.remove();
  const row = tagAuto.querySelector(".tagInputRow");
  if (row) row.remove();
  tagAuto.classList.remove("kbFixed");
  tagAuto.style.bottom = "";
  lbAddTagBtn.style.display = "flex";
}

function openTagDropdown() {
  lbAddTagBtn.style.display = "none";
  const row = document.createElement("div");
  row.className = "tagInputRow";
  const input = document.createElement("input");
  input.placeholder = "Nome da tag";
  input.autofocus = true;
  const doneBtn = document.createElement("button");
  doneBtn.type = "button";
  doneBtn.className = "tagDoneBtn";
  doneBtn.textContent = "✓";
  doneBtn.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const val = input.value.trim();
    input.blur();
    if (val) applyTag(val);
    else closeTagDropdown();
  });
  row.appendChild(input);
  row.appendChild(doneBtn);
  tagAuto.insertBefore(row, tagAuto.firstChild);
  input.focus();

  let debounceTimer = null;
  input.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => fetchTagMatches(input.value), 180);
  });
  input.addEventListener("blur", () => setTimeout(closeTagDropdown, 150));
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const val = input.value.trim();
    if (!val) return;
    applyTag(val, { keepOpen: true });
    input.value = "";
    fetchTagMatches("");
  });
  fetchTagMatches("");
  setTimeout(positionTagAutoForKeyboard, 60);
}

// iOS webviews resize the visual viewport (not layout viewport) when the
// keyboard opens, so the dropdown's `top:100%` position ends up rendered
// behind the keyboard. Detect the keyboard gap and, when present, pin the
// input+dropdown as a fixed block right above it with the dropdown flipped
// to open upward.
function positionTagAutoForKeyboard() {
  if (!tagAuto.querySelector(".tagInputRow")) return;
  const vv = window.visualViewport;
  if (!vv) return;
  const kbGap = window.innerHeight - (vv.height + vv.offsetTop);
  if (kbGap > 60) {
    tagAuto.classList.add("kbFixed");
    tagAuto.style.bottom = (kbGap + 8) + "px";
  } else {
    tagAuto.classList.remove("kbFixed");
    tagAuto.style.bottom = "";
  }
}

if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", positionTagAutoForKeyboard);
}

async function fetchTagMatches(q) {
  let data;
  try {
    const res = await fetch(`/api/gallery/${token}/tags?q=${encodeURIComponent(q)}&limit=20`);
    data = await res.json();
  } catch (e) {
    return;
  }
  const existing = tagAuto.querySelector(".drop");
  if (existing) existing.remove();
  const drop = document.createElement("div");
  drop.className = "drop";
  for (const tag of data.tags) {
    const row = document.createElement("div");
    row.textContent = tag.name;
    row.addEventListener("mousedown", (e) => { e.preventDefault(); applyTag(tag.name); });
    drop.appendChild(row);
  }
  if (q.trim() && !data.exact_match) {
    const row = document.createElement("div");
    row.className = "create";
    row.textContent = `＋ Criar tag '${q.trim()}'`;
    row.addEventListener("mousedown", (e) => { e.preventDefault(); applyTag(q.trim()); });
    drop.appendChild(row);
  }
  tagAuto.appendChild(drop);
  positionTagAutoForKeyboard();
}

lbAddTagBtn.addEventListener("click", openTagDropdown);

lbDelete.addEventListener("click", (e) => {
  e.stopPropagation();
  confirmModal.style.display = "flex";
});
confirmCancel.addEventListener("click", () => { confirmModal.style.display = "none"; });
confirmDelete.addEventListener("click", async () => {
  if (!currentImageId) return;
  try {
    await fetch(`/api/gallery/${token}/image/${currentImageId}`, { method: "DELETE" });
  } catch (e) {}
  confirmModal.style.display = "none";
  closeLightbox();
  loadBlocks();
});

let touchStartY = null;
lightbox.querySelector(".lbBody").addEventListener("touchstart", (e) => { touchStartY = e.touches[0].clientY; });
lightbox.querySelector(".lbBody").addEventListener("touchend", (e) => {
  if (touchStartY !== null && e.changedTouches[0].clientY - touchStartY > 60) closeLightbox();
  touchStartY = null;
});
lightbox.querySelector(".lbBody").addEventListener("click", closeLightbox);

async function loadBlocks() {
  if (!token) {
    setStatus("Link sem token — abra pelo comando /images.", "err");
    return;
  }
  renderSkeleton();
  try {
    const res = await fetch(`/api/gallery/${token}/blocks`);
    if (!res.ok) {
      if (res.status === 401) setStatus("Link expirado. Peça um novo com /images.", "err");
      else setStatus("Não foi possível carregar a galeria.", "err");
      galleryEl.innerHTML = "";
      return;
    }
    const data = await res.json();
    setStatus("", "");
    renderBlocks(data.blocks);
  } catch (e) {
    setStatus("Sem conexão. Toque para tentar de novo.", "err");
    galleryEl.innerHTML = "";
    statusEl.style.cursor = "pointer";
    statusEl.onclick = loadBlocks;
  }
}

function uploadStaged() {
  if (!staged.length) return;
  saveBtn.disabled = true;
  if (tg && tg.MainButton) tg.MainButton.showProgress(true);
  setStatus("", "");
  progressWrap.style.display = "flex";
  progressFill.style.width = "0%";
  progressLabel.textContent = "Enviando… 0%";

  const fd = new FormData();
  for (const s of staged) fd.append("files", s.file);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/gallery/${token}/upload`);
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    progressFill.style.width = pct + "%";
    progressLabel.textContent = `Enviando… ${pct}%`;
  };
  xhr.onload = () => {
    progressWrap.style.display = "none";
    saveBtn.disabled = false;
    if (tg && tg.MainButton) tg.MainButton.hideProgress();
    if (xhr.status < 200 || xhr.status >= 300) {
      if (xhr.status === 401) setStatus("Link expirado. Peça um novo com /images.", "err");
      else setStatus("Falha no envio: " + (xhr.responseText || xhr.status), "err");
      return;
    }
    const data = JSON.parse(xhr.responseText);
    setStatus(`${data.image_count} fotos enviadas com sucesso`, "ok");
    setTimeout(() => setStatus("", ""), 2000);
    galleryEl.prepend(renderBlock({
      id: data.block_id, image_count: data.image_count,
      created_at: new Date().toISOString().replace("Z", ""),
      images: data.images,
    }));
    emptyEl.style.display = "none";
    staged.forEach(s => URL.revokeObjectURL(s.url));
    staged = [];
    renderStaged();
    updateConfirmButton();
  };
  xhr.onerror = () => {
    progressWrap.style.display = "none";
    saveBtn.disabled = false;
    if (tg && tg.MainButton) tg.MainButton.hideProgress();
    setStatus("Sem conexão. Toque para tentar de novo.", "err");
  };
  xhr.send(fd);
}

saveBtn.addEventListener("click", uploadStaged);

if (tg) {
  tg.ready && tg.ready();
  tg.expand && tg.expand();
  if (tg.MainButton) tg.MainButton.onClick(uploadStaged);
}

loadBlocks();
</script>
</body>
</html>
"""
