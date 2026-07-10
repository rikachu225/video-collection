"""Thumbnail route must serve cacheable responses (Cache-Control + ETag)."""
import server


def test_thumbnail_sends_cache_headers(tmp_path, monkeypatch):
    # Fake a resolvable video and a pre-generated thumbnail
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"\x00")
    thumb_dir = tmp_path / "thumbnails"
    thumb_dir.mkdir()
    (thumb_dir / "clip.mp4.jpg").write_bytes(b"\xff\xd8\xff\xd9")  # minimal JPEG

    monkeypatch.setattr(server, "_resolve_video_path", lambda p: fake_video)
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    client = server.app.test_client()
    resp = client.get("/api/thumbnail/clip.mp4")

    assert resp.status_code == 200
    assert "max-age=604800" in resp.headers.get("Cache-Control", "")
    assert resp.headers.get("ETag")


def test_placeholder_gets_short_cache(tmp_path, monkeypatch):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"\x00")
    monkeypatch.setattr(server, "_resolve_video_path", lambda p: fake_video)
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    # No thumbnail on disk and no ffmpeg -> placeholder path
    monkeypatch.setenv("PATH", "")

    client = server.app.test_client()
    resp = client.get("/api/thumbnail/clip.mp4")

    assert resp.status_code == 200
    assert "max-age=300" in resp.headers.get("Cache-Control", "")
