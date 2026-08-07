"""Audio MIME-type registration + extension helper.

2026-08-03 ce-debug (JBL Charge 5 no audio via Chromecast/DLNA): `.flac` (and
several other audio containers) are absent from Python's default `mimetypes`
map, so the `/api/stream` proxy (Starlette `FileResponse`) served a local FLAC
as `application/octet-stream` — which strict Cast/DLNA renderers reject.
Importing `app.transcode` registers the missing types; `container_ext` resolves
the authoritative extension from a part path (not a proxy URL whose extension
hides in the query string)."""

import mimetypes


def test_flac_mimetype_registered():
    import app.transcode  # noqa: F401 — import triggers registration
    assert mimetypes.guess_type("x.flac")[0] == "audio/flac"


def test_other_audio_mimetypes_registered():
    import app.transcode  # noqa: F401
    assert mimetypes.guess_type("x.opus")[0] == "audio/opus"
    assert mimetypes.guess_type("x.m4a")[0] == "audio/mp4"
    assert mimetypes.guess_type("x.ogg")[0] == "audio/ogg"


def test_container_ext_helper():
    from app.transcode import container_ext
    # Authoritative part path / stream_key → the real extension.
    assert container_ext("local-x:Artist/Album/02 Song.flac") == "flac"
    assert container_ext("/library/parts/1/2/file.ogg") == "ogg"
    assert container_ext("http://plex/file.mp3?token=x") == "mp3"
    # A proxy URL hides the extension in the query → no usable ext (this is why
    # the backends must resolve from part_path, not the proxy URL).
    assert container_ext("http://192.168.1.50/api/stream?key=a%2Fb.flac") == ""
    # A host with dots in the path must not be mistaken for an extension.
    assert container_ext("http://192.168.1.50/api/stream") == ""
    assert container_ext("noext") == ""
    assert container_ext("") == ""
