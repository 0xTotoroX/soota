from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from soota.errors import AuthBudgetError, InputError, SourceError
from soota.media import MediaDescriptor, MediaService
from soota.media.download import download_page
from soota.providers.process import ProcessResult


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for real media verification")
    root = tmp_path_factory.mktemp("synthetic-media")

    def run(args):
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *args], check=True, capture_output=True, timeout=30)

    run(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(root / "sample.mp4"),
        ]
    )
    run(["-i", str(root / "sample.mp4"), "-map", "0:v:0", "-c", "copy", str(root / "silent.mp4")])
    run(["-i", str(root / "sample.mp4"), "-map", "0:a:0", "-c", "copy", str(root / "audio.m4a")])
    run(["-i", str(root / "sample.mp4"), "-t", "1", "-map", "0:a:0", "-c", "copy", str(root / "short.m4a")])
    run(["-f", "lavfi", "-i", "color=c=red:size=32x32", "-frames:v", "1", "-threads", "1", str(root / "image.png")])
    run(["-i", str(root / "sample.mp4"), "-c", "copy", "-movflags", "+faststart", str(root / "faststart.mp4")])
    return root


def test_local_media_is_verified_and_original_neighbors_are_preserved(settings, samples, tmp_path):
    source = tmp_path / "movie.mp4"
    source.write_bytes((samples / "sample.mp4").read_bytes())
    neighbor = source.with_suffix(".m4a")
    neighbor.write_bytes(b"user-owned file")
    service = MediaService(settings)
    video = service.from_file(source, expected_duration=3, min_height=180)
    audio = service.extract_audio(video)
    assert video.path != source and video.path.is_relative_to(service.root)
    assert video.kind == "video" and video.verification.duration_checked
    assert video.verification.completeness == "duration_matched"
    assert audio.kind == "audio" and 2.9 < audio.duration_seconds < 3.2
    assert {stream.type for stream in audio.streams} == {"audio"}
    assert video.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert neighbor.read_bytes() == b"user-owned file"
    assert source.read_bytes() == (samples / "sample.mp4").read_bytes()
    assert not list((service.root / ".staging").iterdir())
    assert service.from_file(source, expected_duration=3, min_height=180).cache_hit


def test_local_video_can_produce_audio_without_creating_adjacent_files(settings, samples):
    service = MediaService(settings)
    asset = service.from_file(samples / "sample.mp4", kind="audio", expected_duration=3)
    assert asset.kind == "audio" and asset.path.suffix == ".m4a"
    assert not (samples / "sample.m4a").exists()


def test_unknown_source_duration_is_not_claimed_complete(settings, samples):
    asset = MediaService(settings).from_file(samples / "sample.mp4")
    assert asset.verification.decoded
    assert not asset.verification.duration_checked
    assert asset.verification.completeness == "source_duration_unknown"


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"expected_duration": 20}, "时长"),
        ({"min_height": 720}, "最低画面高度"),
        ({"kind": "image"}, "类型"),
    ],
)
def test_requested_media_contract_is_enforced(settings, samples, kwargs, message):
    service = MediaService(settings)
    with pytest.raises(SourceError, match=message):
        service.from_file(samples / "sample.mp4", **kwargs)
    assert not list((service.root / "objects").rglob("*.*"))


def test_silent_video_requires_explicit_permission(settings, samples):
    service = MediaService(settings)
    with pytest.raises(SourceError, match="音轨"):
        service.from_file(samples / "silent.mp4")
    assert service.from_file(samples / "silent.mp4", allow_silent=True).kind == "video"


def test_mux_validates_each_track_and_reuses_managed_output(settings, samples):
    service = MediaService(settings)
    result = service.mux(samples / "silent.mp4", samples / "audio.m4a", expected_duration=3)
    assert result.kind == "video" and {s.type for s in result.streams} == {"video", "audio"}
    assert result.verification.duration_checked
    assert service.mux(samples / "silent.mp4", samples / "audio.m4a", expected_duration=3).cache_hit
    with pytest.raises(SourceError, match="时长"):
        service.mux(samples / "silent.mp4", samples / "short.m4a", expected_duration=3)


def test_header_valid_truncated_media_does_not_publish(settings, samples, tmp_path):
    data = (samples / "faststart.mp4").read_bytes()
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(data[: len(data) // 2])
    service = MediaService(settings)
    with pytest.raises(SourceError):
        service.from_file(truncated, expected_duration=3)
    assert not list((service.root / "objects").rglob("*.*"))


def test_limits_are_checked_on_actual_media(settings, samples):
    with pytest.raises(SourceError, match="字节"):
        MediaService(settings, max_bytes=100).from_file(samples / "sample.mp4")
    with pytest.raises(SourceError, match="时长"):
        MediaService(settings, max_duration_seconds=1).from_file(samples / "sample.mp4")
    with pytest.raises(InputError):
        MediaService(settings, max_duration_seconds=float("nan"))


def test_image_content_is_checked_not_its_suffix(settings, samples, tmp_path):
    disguised = tmp_path / "wrong.mp4"
    disguised.write_bytes((samples / "image.png").read_bytes())
    service = MediaService(settings)
    image = service.from_file(disguised, kind="image")
    assert image.kind == "image" and image.path.suffix == ".png"
    assert image.streams[0].width == image.streams[0].height == 32
    disguised.write_text("<html>login required</html>")
    with pytest.raises(SourceError):
        service.from_file(disguised, kind="image")


def test_changed_asset_cannot_be_used_with_stale_verification(settings, samples):
    service = MediaService(settings)
    asset = service.from_file(samples / "sample.mp4")
    asset.path.write_bytes((samples / "silent.mp4").read_bytes())
    with pytest.raises(SourceError, match="发生变化"):
        service.extract_audio(asset)


def test_managed_collision_never_overwrites_existing_file(settings, samples):
    service = MediaService(settings)
    asset = service.from_file(samples / "sample.mp4")
    asset.path.write_bytes(b"existing unrelated content")
    with pytest.raises(SourceError, match="未覆盖"):
        service.from_file(samples / "sample.mp4")
    assert asset.path.read_bytes() == b"existing unrelated content"


def test_remote_tracks_resume_without_repeating_successful_download(settings, samples):
    requests = []
    unavailable = True

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/audio" and unavailable:
            return httpx.Response(503)
        name = "silent.mp4" if request.url.path == "/video" else "audio.m4a"
        return httpx.Response(200, content=(samples / name).read_bytes())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = MediaService(settings, http_client=client)
        descriptor = MediaDescriptor(
            video_url="https://example.test/video?token=private",
            audio_url="https://example.test/audio?token=private",
            source_provider="example",
        )
        with pytest.raises(SourceError):
            service.from_descriptor(descriptor, expected_duration=3)
        unavailable = False
        result = service.from_descriptor(descriptor, expected_duration=3)
    assert result.kind == "video" and result.source_provider == "example"
    assert requests == ["/video", "/audio", "/audio"]


def test_signed_urls_and_headers_are_not_written_to_receipts(settings, samples):
    secret = "private-secret-value"
    received = []

    def handler(request):
        received.append(request.headers["Authorization"])
        return httpx.Response(200, content=(samples / "sample.mp4").read_bytes())

    descriptor = MediaDescriptor(
        url=f"https://example.test/file?signature={secret}",
        headers={"Authorization": secret},
        source_provider="example",
    )
    assert secret not in repr(descriptor) and secret not in descriptor.model_dump_json()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = MediaService(settings, http_client=client)
        asset = service.from_descriptor(descriptor, expected_duration=3)
    assert received == [secret]
    assert secret not in asset.model_dump_json()
    assert all(secret not in file.read_text() for file in (service.root / "operations").glob("*.json"))


@pytest.mark.parametrize(
    "response,message",
    [
        (lambda: httpx.Response(200, content=b"<html>login</html>"), "媒体"),
        (lambda: httpx.Response(200, headers={"content-length": "999"}, content=b"short"), "长度"),
        (lambda: httpx.Response(206, content=b"partial"), "完整文件"),
        (lambda: httpx.Response(403), "拒绝访问"),
    ],
)
def test_http_success_status_is_not_download_success(settings, response, message):
    with httpx.Client(transport=httpx.MockTransport(lambda request: response())) as client:
        service = MediaService(settings, http_client=client)
        with pytest.raises((SourceError, AuthBudgetError), match=message):
            service.from_descriptor(MediaDescriptor(url="https://example.test/media?signature=secret"))
    assert not list((service.root / "operations").glob("*.json"))
    assert not list((service.root / ".staging").iterdir())


def test_http_stream_size_limit_and_cross_origin_headers(settings):
    headers = []

    def handler(request):
        headers.append(dict(request.headers))
        if request.url.host == "one.test":
            return httpx.Response(302, headers={"Location": "https://two.test/media"})
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * 200))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = MediaService(settings, max_bytes=100, http_client=client)
        with pytest.raises(SourceError, match="字节"):
            service.from_descriptor(MediaDescriptor(url="https://one.test/media", headers={"X-Secret": "private"}))
    assert headers[0]["x-secret"] == "private" and "x-secret" not in headers[1]


def test_page_download_uses_final_path_and_strict_fragment_completion(settings, samples, monkeypatch):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        output = Path(argv[argv.index("-o") + 1]).parent / "media.mkv"
        shutil.copyfile(samples / "sample.mp4", output)
        return ProcessResult(0, json.dumps(str(output)) + "\n3\n", "")

    monkeypatch.setattr("soota.media.probe.run_process", runner)
    settings.cache_dir.mkdir(parents=True)
    path, duration = download_page(
        settings, "https://example.test/watch", settings.cache_dir, kind="video", max_bytes=10000000, timeout=5
    )
    assert path.suffix == ".mkv" and duration == 3
    argv = calls[0]
    assert "--abort-on-unavailable-fragments" in argv
    assert argv[argv.index("--fragment-retries") + 1] == "0"
    assert "--skip-download" not in argv
    assert "after_move:%(filepath)j" in argv


def test_download_tool_error_does_not_expose_signed_input(settings, monkeypatch, tmp_path):
    def runner(argv, **kwargs):
        return ProcessResult(1, "", "https://example.test?signature=private")

    monkeypatch.setattr("soota.media.probe.run_process", runner)
    with pytest.raises(SourceError) as failure:
        download_page(
            settings, "https://example.test?signature=private", tmp_path, kind="video", max_bytes=100, timeout=1
        )
    assert "private" not in str(failure.value.to_dict())


def test_manifest_login_page_is_not_passed_to_page_extractor(settings, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("manifest failure must not trigger page extraction")

    monkeypatch.setattr("soota.media.service.download_page", forbidden)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>login</html>"))
    ) as client:
        service = MediaService(settings, http_client=client)
        with pytest.raises(SourceError, match="不是 HLS 或 DASH"):
            service.from_descriptor(MediaDescriptor(url="https://example.test/manifest.mpd"))


def test_invalid_request_is_rejected_before_network(settings):
    def forbidden(request):
        pytest.fail("invalid request must not reach the network")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        service = MediaService(settings, http_client=client)
        descriptor = MediaDescriptor(url="https://example.test/media")
        with pytest.raises(InputError):
            service.from_descriptor(descriptor, expected_duration=float("nan"))
        with pytest.raises(InputError):
            service.from_descriptor(descriptor, kind="audio", min_height=720)


def test_os_limit_bounds_unknown_length_tool_outputs(tmp_path):
    import sys

    from soota.media.probe import command

    output = tmp_path / "oversized.media"
    with pytest.raises(SourceError):
        command(
            [sys.executable, "-c", "import sys;open(sys.argv[1],'wb').write(b'x'*1048576)", str(output)],
            timeout=10,
            limit_file_bytes=1024,
        )
    if sys.platform == "win32":
        assert not output.exists()  # No dispatch without an enforceable hard file limit.
    else:
        assert output.stat().st_size <= 1024


def test_audio_conversion_does_not_claim_upstream_duration_was_known(settings, samples):
    service = MediaService(settings)
    original = service.from_file(samples / "sample.mp4")
    converted = service.extract_audio(original)
    assert converted.verification.completeness == "input_duration_matched"
    assert service.extract_audio(original).verification.completeness == "input_duration_matched"
