import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from soota.catalog import catalog, estimate_catalog, resume_catalog
from soota.content.wechat import parse_article
from soota.errors import ContractError, RemoteDeleted, SourceError, VerificationRequired
from soota.models import CatalogJob
from soota.providers import wechat
from soota.providers.tikhub.catalog import WECHAT_ACCOUNT_ARTICLES, parse_catalog_page
from soota.routing import recognize
from soota.storage.jobs import JobStore
from soota.tasks.inputs import parse_outputs
from soota.tasks.models import InputSpec, TaskSpec
from soota.tasks.runner import execute

FIXTURES = Path(__file__).parents[1] / "fixtures"
BIZ = "TXpFeGFtcGxl"
URL = f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid=10001&idx=1&sn=exampleSignature"
ALBUM = f"https://mp.weixin.qq.com/mp/appmsgalbum?__biz={BIZ}&album_id=9001"


def fixture(name):
    return (FIXTURES / name).read_text()


def row(mid=10001, idx=1, pos=1):
    return {
        "url": URL.replace("10001", str(mid)).replace("idx=1", f"idx={idx}"),
        "msgid": str(mid),
        "itemidx": str(idx),
        "pos_num": pos,
        "title": f"文章 {pos}",
        "create_time": 1767225600,
    }


def album(rows, more=0, total=2):
    return {
        "base_resp": {"ret": 0},
        "getalbum_resp": {"article_list": rows, "continue_flag": more, "total_count": total, "album_name": "合成合集"},
    }


def test_article_body_metadata_code_images_and_album_list():
    result = parse_article(fixture("wechat_article.html"), "https://mp.weixin.qq.com/s/synthetic")
    assert result.title == "示例文章 & 代码"
    assert result.raw["author"] == "示例公众号"
    assert result.raw["publish_time"] == "2026-01-01T00:00:00+00:00"
    assert result.raw["album_ids"] == ["9001", "9002", "9003"]
    assert result.raw["__biz"] == BIZ and result.raw["mid"] == "10001"
    assert '````python\nif a < 2:\n    print("``` & 保留")\n````' in result.body
    assert "![示例图](https://mmbiz.qpic.cn/example/image?wx_fmt=png)" in result.body
    assert result.body.index("第一段") < result.body.index("最后一段")
    assert "赞赏噪声" not in result.body and "二维码噪声" not in result.body


def test_dom_date_fallback_and_conflicting_identity():
    body = fixture("wechat_article.html").replace('var create_time = "1767225600";', "")
    assert parse_article(body, URL).raw["publish_time"] == "2026-01-01T12:00:00+08:00"
    with pytest.raises(ContractError, match="身份"):
        parse_article(body, URL.replace("10001", "99999"))


@pytest.mark.parametrize(
    "name,exception", [("wechat_deleted.html", RemoteDeleted), ("wechat_verification.html", VerificationRequired)]
)
def test_deleted_and_verification_are_distinct_non_retry_errors(name, exception):
    with pytest.raises(exception) as exc:
        parse_article(fixture(name), URL)
    assert exc.value.details["retryable"] is False
    if exception is RemoteDeleted:
        assert exc.value.details["reason"] == "publisher_deleted"
        assert len(exc.value.details["response_sha256"]) == 64


def test_policy_removed_unknown_and_script_templates():
    with pytest.raises(RemoteDeleted):
        parse_article("<h2>此内容因违规无法查看</h2>", URL)
    with pytest.raises(ContractError):
        parse_article('<script>var template="该内容已被发布者删除";</script><h2>未知页面</h2>', URL)


def test_anonymous_http_drops_cookies_and_reads_verification_403(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert "Mozilla/5.0" in request.headers["user-agent"]
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": URL, "set-cookie": "server=value"})
        return httpx.Response(403, text=fixture("wechat_verification.html"))

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(VerificationRequired):
        wechat.fetch(URL)
    assert len(calls) == 2


def test_default_ingest_creates_real_source_bundle_without_browser(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(wechat, "fetch", lambda url: calls.append(url) or fixture("wechat_article.html"))
    spec = TaskSpec(
        inputs=[InputSpec(kind="url", value="https://mp.weixin.qq.com/s/synthetic")],
        outputs=parse_outputs("context,markdown,data"),
        output=settings.output_dir,
        max_cost=0,
    )
    result = execute(settings, spec)
    assert result.ok, result.model_dump()
    assert len(calls) == 1
    manifests = list(settings.output_dir.rglob("manifest.json"))
    manifest = json.loads(manifests[0].read_text())
    assert manifest["processing"]["provider"] == "http"
    assert manifest["metadata"]["creator"] == "示例公众号"
    assert manifest["metadata"]["provenance"]["wechat"]["mid"] == "10001"
    assert (manifests[0].parent / "body.md").is_file()
    assert (manifests[0].parent / "context.md").is_file()


def test_album_pages_and_resume_remainder(settings, monkeypatch):
    calls = []

    def fetch(url):
        q = parse_qs(urlsplit(url).query)
        calls.append(q)
        assert q["f"] == ["json"] and q["action"] == ["getalbum"]
        if "begin_msgid" not in q:
            return json.dumps(album([row(pos=1), row(10002, 2, 2)], more=1, total=3))
        assert q["begin_msgid"] == ["10002"] and q["begin_itemidx"] == ["2"]
        return json.dumps(album([row(10003, 1, 3)], total=3))

    monkeypatch.setattr(wechat, "fetch", fetch)
    assert estimate_catalog(settings, ALBUM, max_cost=0).data["estimated_cost_usd"] == 0
    first = catalog(settings, ALBUM, max_items=1, max_cost=0)
    assert first.data["pending_items"] == 1 and len(calls) == 1
    result = resume_catalog(settings, first.data["job_id"], max_items=10)
    assert result.ok and len(calls) == 2 and result.data["reserved_cost_usd"] == 0
    payload = json.loads(Path(result.data["catalog"]).read_text())
    assert payload["total_items"] == 3
    assert [item["extras"]["pos_num"] for item in payload["items"]] == ["1", "2", "3"]


def test_album_repeated_cursor_and_empty_unknown_page_fail(settings, monkeypatch):
    monkeypatch.setattr(wechat, "fetch", lambda url: json.dumps(album([row()], more=1)))
    result = catalog(settings, ALBUM, max_cost=0)
    assert not result.ok and result.data["error"]["code"] == "contract"
    for payload in [album([], more=1), {"base_resp": {"ret": -1}}, {"base_resp": {"ret": 0}}]:
        with pytest.raises((ContractError, SourceError)):
            wechat.album_page(recognize(ALBUM), payload)


def test_account_post_body_budget_and_resume(settings, monkeypatch):
    from soota.providers.quotes import cached_quote, namespace

    calls = []
    monkeypatch.setattr("soota.providers.tikhub.client.load_tikhub_api_key", lambda: None)
    cached_quote(
        settings,
        "tikhub",
        namespace(settings.tikhub.base_url, ""),
        WECHAT_ACCOUNT_ARTICLES,
        lambda: {"estimated_cost_usd": 0.01},
    )

    def request(method, path, params, **kwargs):
        assert method == "POST" and path == WECHAT_ACCOUNT_ARTICLES and not params
        body = kwargs["json"]
        assert body["username"] == "gh_example" and body["raw"] is False
        calls.append(body["offset"])
        job = CatalogJob.model_validate(JobStore(settings.jobs_dir).list_jobs()[0])
        assert len(job.requests) == len(calls)  # 派发之前已持久预留。
        return {
            "code": 200,
            "data": {
                "biz_username": "gh_example",
                "is_end": bool(body["offset"]),
                "count": 1,
                "next_offset": None if body["offset"] else "next+/=",
                "articles": [row(10002 if body["offset"] else 10001)],
            },
        }

    denied = catalog(settings, "gh_example", provider="tikhub", max_cost=0, requester=request)
    assert not denied.ok and not calls
    result = catalog(settings, "gh_example", provider="tikhub", max_cost=0.01, requester=request)
    assert not result.ok and calls == [""] and result.data["reserved_cost_usd"] == 0.01
    result = resume_catalog(settings, result.data["job_id"], max_cost=0.02, requester=request)
    assert result.ok and calls == ["", "next+/="] and result.data["item_count"] == 2


@pytest.mark.parametrize(
    "data",
    [
        {"biz_username": "gh_wrong", "articles": [], "count": 0, "is_end": 1},
        {"biz_username": "gh_example", "articles": [], "count": 0, "is_end": 0},
        {"biz_username": "gh_example", "articles": [], "count": 0},
    ],
)
def test_account_contract_drift_is_not_empty_success(data):
    with pytest.raises(ContractError):
        parse_catalog_page(recognize("gh_example"), {"code": 200, "data": data}, None, 20)


def test_remote_deleted_resume_skips_but_force_rechecks(settings, monkeypatch):
    from soota.tasks.runner import resume

    calls = []
    monkeypatch.setattr(wechat, "fetch", lambda url: calls.append(url) or fixture("wechat_deleted.html"))
    spec = TaskSpec(
        inputs=[InputSpec(kind="url", value=URL)],
        outputs=parse_outputs("context,data"),
        output=settings.output_dir,
        max_cost=0,
    )
    first = execute(settings, spec)
    assert not first.ok and len(calls) == 1
    assert first.data["items"][0]["errors"][0]["code"] == "remote_deleted"
    resumed = resume(settings, first.data["job_id"])
    assert not resumed.ok and len(calls) == 1
    assert resumed.data["items"][0]["errors"][0]["code"] == "remote_deleted"
    resume(settings, first.data["job_id"], force=True)
    assert len(calls) == 2


def test_http_article_strips_incidental_credentials(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(wechat, "fetch", lambda url: calls.append(url) or fixture("wechat_article.html"))
    wechat.capture(URL + "&pass_ticket=synthetic&uin=123&key=synthetic")
    assert calls == [URL]


def test_images_include_referer_and_retain_article_metadata(settings, monkeypatch, tmp_path):
    import hashlib

    from soota.media.models import MediaAsset, MediaVerification
    from soota.media.service import MediaService

    monkeypatch.setattr(wechat, "fetch", lambda url: fixture("wechat_article.html"))
    image = tmp_path / "source.png"
    image.write_bytes(b"synthetic image bytes")
    calls = []

    def download(self, descriptor, *, kind):
        calls.append(descriptor)
        assert kind == "image" and descriptor.headers["Referer"] == URL
        assert "cookie" not in {key.lower() for key in descriptor.headers}
        return MediaAsset(
            path=image,
            kind="image",
            format="png",
            size=image.stat().st_size,
            sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
            streams=[],
            verification=MediaVerification(),
            source_provider="http",
        )

    monkeypatch.setattr(MediaService, "from_descriptor", download)
    spec = TaskSpec(
        inputs=[InputSpec(kind="url", value=URL)],
        outputs=parse_outputs("context,images"),
        output=settings.output_dir,
        max_cost=0,
    )
    result = execute(settings, spec)
    assert result.ok, result.model_dump()
    assert len(calls) == 1
    path = next(settings.output_dir.rglob("manifest.json"))
    saved = json.loads(path.read_text())
    assert saved["metadata"]["published_at"] == "2026-01-01T00:00:00+00:00"
    assert (path.parent / "attachments/image-001.png").read_bytes() == image.read_bytes()
    assert "attachments/image-001.png" in (path.parent / "body.md").read_text()
