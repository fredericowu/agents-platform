"""Telegram Image Gallery — /blocks images-array contract test.

Covers the backward-compatible extension added for the UX-redesign card:
GET /api/gallery/{token}/blocks must now include an ordered images[] array
per block (ids only), alongside the pre-existing id/image_count/created_at
fields, so historical blocks can render real thumbnails on every load.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture(scope="module")
def db_engine():
    import backend.app.db as db_mod

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    orig_engine = db_mod.engine
    orig_session = db_mod.SessionLocal

    db_mod.engine = eng
    db_mod.SessionLocal = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)

    db_mod.init_db()

    yield eng

    db_mod.engine = orig_engine
    db_mod.SessionLocal = orig_session
    eng.dispose()


@pytest.fixture(scope="module")
def client(db_engine):
    import backend.app.db as db_mod
    from backend.app.db import get_session
    from backend.app.main import app
    from fastapi.testclient import TestClient

    def _override():
        s = db_mod.SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = _override
    c = TestClient(app, raise_server_exceptions=True)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture
def db_session(db_engine):
    import backend.app.db as db_mod
    s = db_mod.SessionLocal()
    yield s
    s.close()


def _make_token(db_session):
    from backend.app.models import GalleryToken

    tok = GalleryToken(
        bot_slug="crispal-bot", origin_chat_id="123", created_by="1",
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db_session.add(tok)
    db_session.commit()
    db_session.refresh(tok)
    return tok


def _make_block(db_session, token, n_images):
    from backend.app.models import GalleryBlock, GalleryImage

    block = GalleryBlock(token=token.token, bot_slug=token.bot_slug,
                          origin_chat_id=token.origin_chat_id, image_count=n_images)
    db_session.add(block)
    db_session.flush()
    image_ids = []
    for i in range(n_images):
        img = GalleryImage(id=f"{block.id}-img{i}", block_id=block.id,
                            file_path=f"/tmp/{block.id}-{i}.jpg",
                            original_name=f"photo{i}.jpg", mime="image/jpeg", bytes=100)
        db_session.add(img)
        image_ids.append(img.id)
    db_session.commit()
    return block, image_ids


def test_blocks_include_ordered_images_per_block(client, db_session):
    token = _make_token(db_session)
    block_a, images_a = _make_block(db_session, token, 3)
    block_b, images_b = _make_block(db_session, token, 2)

    res = client.get(f"/api/gallery/{token.token}/blocks")
    assert res.status_code == 200
    blocks = {b["id"]: b for b in res.json()["blocks"]}

    assert blocks[block_a.id]["image_count"] == 3
    assert [img["id"] for img in blocks[block_a.id]["images"]] == images_a
    assert blocks[block_b.id]["image_count"] == 2
    assert [img["id"] for img in blocks[block_b.id]["images"]] == images_b
    # legacy fields unchanged
    assert "created_at" in blocks[block_a.id]


def test_blocks_rejects_invalid_token(client):
    res = client.get("/api/gallery/not-a-real-token/blocks")
    assert res.status_code == 401


# --- delete image -----------------------------------------------------------

def test_delete_image_removes_file_and_row_and_decrements_count(client, db_session, tmp_path):
    from backend.app.models import GalleryImage

    token = _make_token(db_session)
    block, image_ids = _make_block(db_session, token, 2)
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"fake-bytes")
    img = db_session.get(GalleryImage, image_ids[0])
    img.file_path = str(f)
    db_session.commit()

    res = client.delete(f"/api/gallery/{token.token}/image/{image_ids[0]}")
    assert res.status_code == 200
    body = res.json()
    assert body == {"ok": True, "block_id": block.id, "block_deleted": False, "image_count": 1}
    assert not f.exists()
    assert db_session.get(GalleryImage, image_ids[0]) is None
    assert db_session.get(GalleryImage, image_ids[1]) is not None


def test_delete_last_image_removes_block(client, db_session):
    token = _make_token(db_session)
    block, image_ids = _make_block(db_session, token, 1)
    block_id = block.id

    res = client.delete(f"/api/gallery/{token.token}/image/{image_ids[0]}")
    assert res.status_code == 200
    body = res.json()
    assert body["block_deleted"] is True
    assert body["image_count"] == 0

    blocks_res = client.get(f"/api/gallery/{token.token}/blocks")
    assert block_id not in {b["id"] for b in blocks_res.json()["blocks"]}


def test_delete_image_rejects_invalid_token(client, db_session):
    token = _make_token(db_session)
    _, image_ids = _make_block(db_session, token, 1)
    res = client.delete(f"/api/gallery/not-a-real-token/image/{image_ids[0]}")
    assert res.status_code == 401


# --- tagging ------------------------------------------------------------

def test_tag_apply_is_idempotent_and_case_insensitive(client, db_session):
    from backend.app.models import GalleryTag

    token = _make_token(db_session)
    _, image_ids = _make_block(db_session, token, 1)
    image_id = image_ids[0]

    res1 = client.post(f"/api/gallery/{token.token}/image/{image_id}/tag", json={"name": "Inverno"})
    assert res1.status_code == 200
    body1 = res1.json()
    assert body1["created"] is True
    assert body1["already_applied"] is False
    assert body1["tag"]["name"] == "Inverno"

    res2 = client.post(f"/api/gallery/{token.token}/image/{image_id}/tag", json={"name": "inverno"})
    assert res2.status_code == 200
    body2 = res2.json()
    assert body2["created"] is False
    assert body2["already_applied"] is True
    assert body2["tag"]["id"] == body1["tag"]["id"]

    tags = db_session.query(GalleryTag).filter(GalleryTag.bot_slug == token.bot_slug).all()
    assert len(tags) == 1


def test_tags_autocomplete_prefix_and_exact_match(client, db_session):
    # Unique tag name — GalleryTag is scoped by bot_slug only (spans all
    # tokens), and the module-scoped db fixture is shared across every test
    # in this file using the same bot_slug, so a common word would collide
    # with tags created by other tests.
    token = _make_token(db_session)
    _, image_ids = _make_block(db_session, token, 1)
    client.post(f"/api/gallery/{token.token}/image/{image_ids[0]}/tag", json={"name": "outonozzq"})

    res = client.get(f"/api/gallery/{token.token}/tags", params={"q": "outonoz"})
    assert res.status_code == 200
    body = res.json()
    assert [t["name"] for t in body["tags"]] == ["outonozzq"]
    assert body["exact_match"] is False

    res2 = client.get(f"/api/gallery/{token.token}/tags", params={"q": "outonozzq"})
    assert res2.json()["exact_match"] is True


def test_remove_tag_from_image_and_gc(client, db_session):
    from backend.app.models import GalleryTag

    token = _make_token(db_session)
    _, image_ids = _make_block(db_session, token, 2)
    tag = client.post(f"/api/gallery/{token.token}/image/{image_ids[0]}/tag", json={"name": "verão"}).json()["tag"]
    client.post(f"/api/gallery/{token.token}/image/{image_ids[1]}/tag", json={"name": "verão"})

    res1 = client.delete(f"/api/gallery/{token.token}/image/{image_ids[0]}/tag/{tag['id']}")
    assert res1.status_code == 200
    assert res1.json()["tag_gc"] is False
    assert db_session.get(GalleryTag, tag["id"]) is not None

    res2 = client.delete(f"/api/gallery/{token.token}/image/{image_ids[1]}/tag/{tag['id']}")
    assert res2.json()["tag_gc"] is True
    assert db_session.get(GalleryTag, tag["id"]) is None


def test_blocks_carry_tags_via_constant_query_count(client, db_session, db_engine):
    from sqlalchemy import event

    def _tag_aggregate_query_count(fn):
        count = [0]

        def _listener(conn, cursor, statement, parameters, context, executemany):
            if "gallery_image_tags" in statement:
                count[0] += 1

        event.listen(db_engine, "before_cursor_execute", _listener)
        try:
            fn()
        finally:
            event.remove(db_engine, "before_cursor_execute", _listener)
        return count[0]

    token_small = _make_token(db_session)
    _, images_small = _make_block(db_session, token_small, 2)
    client.post(f"/api/gallery/{token_small.token}/image/{images_small[0]}/tag", json={"name": "bota"})

    token_big = _make_token(db_session)
    block_big, images_big = _make_block(db_session, token_big, 20)
    for image_id in images_big[:10]:
        client.post(f"/api/gallery/{token_big.token}/image/{image_id}/tag", json={"name": "bota"})

    # Exactly one aggregate tags query per /blocks call, regardless of how
    # many images are on the page — the whole point of the single IN(...)
    # query is to avoid per-image N+1 lookups.
    n_small = _tag_aggregate_query_count(lambda: client.get(f"/api/gallery/{token_small.token}/blocks"))
    n_big = _tag_aggregate_query_count(lambda: client.get(f"/api/gallery/{token_big.token}/blocks"))
    assert n_small == 1
    assert n_big == 1

    res = client.get(f"/api/gallery/{token_big.token}/blocks")
    tagged = [img for img in res.json()["blocks"][0]["images"] if img["tags"]]
    assert len(tagged) == 10
    assert tagged[0]["tags"] == [{"id": tagged[0]["tags"][0]["id"], "name": "bota"}]
