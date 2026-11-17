"""水位读隔离验收：钉住读法「甲」——现行与历史分接口、分缓存键。

覆盖《水位读法说明》第 9 节验收项：
- 历史翻页前后现行不空白、不变；
- 历史读不写 wm:current:*，两接口缓存键不同；
- 缺现行必须显式空态原因；
- 历史条目不冒充现行；
- 混申读返回「水位读口径偏了」并记审计；
- 历史翻页与推现行并发时记「读争用」；
- 本机库与口令体系不变。
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

import app as appmod
from app import app, init_db, connect


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _login(c):
    r = c.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _seed(rows, reset=True):
    with connect() as c:
        if reset:
            # 去掉 init_db 的示例水位，避免 id/条数干扰隔离断言。
            c.execute("DELETE FROM watermarks")
            c.execute("DELETE FROM watermark_read_audit")
        for consumer, biz_date, wm, src in rows:
            c.execute(
                "INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
                (consumer, biz_date, wm, src),
            )
    appmod.WM_CURRENT_CACHE.clear()
    appmod.WM_HISTORY_CACHE.clear()


def _audit_rows(event):
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM watermark_read_audit WHERE event=? ORDER BY id", (event,))]


def test_history_paging_does_not_clear_current():
    _seed([
        ("ads_order", "2026-11-01", "2026-11-01T18:00:00", "SL-1"),
        ("ads_order", "2026-11-02", "2026-11-02T18:00:00", "SL-2"),
        ("ads_order", "2026-11-03", "2026-11-03T18:00:00", "SL-3"),
    ])
    c = TestClient(app)
    h = _login(c)

    first = c.get("/api/watermarks/current", headers=h, params={"consumer": "ads_order"})
    assert first.status_code == 200
    body = first.json()
    assert body["state"] == "ok"
    assert body["item"]["source_slice"] == "SL-3"
    assert body["cache_key"] == "wm:current:ads_order"

    # 历史翻三页，现行必须毫发无损。
    for _ in range(3):
        hist = c.get("/api/watermarks/history", headers=h,
                     params={"consumer": "ads_order", "limit": 1})
        assert hist.status_code == 200
        hb = hist.json()
        assert hb["cache_key"].startswith("wm:history:ads_order|limit=1")
        nxt = hb.get("next_cursor")
        if not nxt:
            break

    after = c.get("/api/watermarks/current", headers=h, params={"consumer": "ads_order"})
    assert after.status_code == 200
    ab = after.json()
    assert ab["state"] == "ok"
    assert ab["item"]["source_slice"] == "SL-3"
    assert ab["count"] == 1

    cache = c.get("/api/watermarks/read-cache", headers=h).json()
    assert cache["isolated"] is True
    assert "wm:current:ads_order" in cache["current_keys"]
    assert all(k.startswith("wm:history:") for k in cache["history_keys"])


def test_current_empty_has_explicit_reason_not_silent_blank():
    c = TestClient(app)
    h = _login(c)
    r = c.get("/api/watermarks/current", headers=h, params={"consumer": "nobody"})
    assert r.status_code == 200
    b = r.json()
    assert b["state"] == "empty"
    assert b["reason_code"] == "no_current_watermark"
    assert b["reason"] and b["reason"].strip()
    assert b["item"] is None and b["items"] == []
    # 空态也要带读模式与缓存键，五处才能对齐。
    assert b["read_mode"] == "current"
    assert b["cache_key"] == "wm:current:nobody"


def test_history_excludes_current_and_never_impersonates_it():
    _seed([
        ("ads_order", "2026-11-01", "2026-11-01T18:00:00", "SL-1"),
        ("ads_order", "2026-11-02", "2026-11-02T18:00:00", "SL-2"),
        ("ads_order", "2026-11-03", "2026-11-03T18:00:00", "SL-3"),
    ])
    c = TestClient(app)
    h = _login(c)
    cur = c.get("/api/watermarks/current", headers=h, params={"consumer": "ads_order"}).json()
    assert cur["item"]["source_slice"] == "SL-3"

    hist = c.get("/api/watermarks/history", headers=h,
                 params={"consumer": "ads_order", "limit": 10}).json()
    assert hist["state"] == "ok"
    assert hist["excluded_current_id"] == cur["item"]["id"]
    assert all(i["row_role"] == "history" for i in hist["items"])
    assert all(i["id"] != cur["item"]["id"] for i in hist["items"])
    assert [i["id"] for i in hist["items"]] == sorted(
        [i["id"] for i in hist["items"]], reverse=True)
    assert [i["source_slice"] for i in hist["items"]] == ["SL-2", "SL-1"]


def test_history_only_current_has_explicit_empty_reason():
    _seed([("ads_order", "2026-11-01", "2026-11-01T18:00:00", "SL-only")])
    c = TestClient(app)
    h = _login(c)
    hist = c.get("/api/watermarks/history", headers=h,
                 params={"consumer": "ads_order", "limit": 10}).json()
    assert hist["state"] == "empty"
    assert hist["reason_code"] == "no_history_only_current"
    assert hist["reason"]
    assert hist["excluded_current_id"] is not None

    # 指定消费方根本没有水位时，历史页也不得把它说成“仅现行一条”。
    ghost = c.get("/api/watermarks/history", headers=h,
                  params={"consumer": "ghost", "limit": 10}).json()
    assert ghost["state"] == "empty"
    assert ghost["reason_code"] == "no_current_watermark"
    assert "ghost" in ghost["reason"]


def test_aggregate_current_returns_one_row_per_consumer():
    _seed([
        ("ads_order", "2026-11-01", "2026-11-01T18:00:00", "SL-1"),
        ("ads_order", "2026-11-02", "2026-11-02T18:00:00", "SL-2"),
        ("ads_user", "2026-11-02", "2026-11-02T19:00:00", "SL-U1"),
    ])
    c = TestClient(app)
    h = _login(c)
    body = c.get("/api/watermarks/current", headers=h).json()
    assert body["state"] == "ok"
    assert body["count"] == 2
    assert {i["source_slice"] for i in body["items"]} == {"SL-2", "SL-U1"}
    assert all(i["row_role"] == "current" for i in body["items"])
    assert body["cache_key"] == "wm:current:*"


def test_five_places_are_aligned_in_frontend():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "frontend" / "index.html").read_text("utf-8")
    for place in ["watermark-page", "history-detail", "release-flow", "list-side-note", "policy-bar"]:
        assert f'data-wm-place="{place}"' in html, place
    # 五处对齐依赖两个分接口与两套键模板，不能只改提示文案。
    assert "/api/watermarks/current" in html
    assert "/api/watermarks/history" in html
    assert "wm:current:*" in html and "wm:history:*" in html
    assert "水位读口径偏了" in html


def test_mixed_read_mode_is_rejected_and_audited():
    c = TestClient(app)
    h = _login(c)

    bad1 = c.get("/api/watermarks/current", headers=h,
                 params={"consumer": "ads_order", "limit": 10})
    assert bad1.status_code == 400
    b1 = bad1.json()
    assert b1["code"] == "水位读口径偏了"
    assert b1["pinned_option"] == "甲"
    assert b1["read_mode"] == "current"

    bad2 = c.get("/api/watermarks/history", headers=h,
                 params={"include_current": "true"})
    assert bad2.status_code == 400
    assert bad2.json()["code"] == "水位读口径偏了"

    bad3 = c.get("/api/watermarks/history", headers={**h, "X-Watermark-Read-Mode": "current"})
    assert bad3.status_code == 400

    audit = c.get("/api/watermarks/read-audit", headers=h).json()
    assert audit["deviation_count"] >= 3
    assert all(r["code"] == "水位读口径偏了" for r in audit["items"] if r["event"] == "deviation")
    policy = c.get("/api/watermarks/read-policy", headers=h).json()
    assert policy["pinned_option"] == "甲"
    assert policy["counts"]["deviation"] >= 3


def test_concurrent_history_and_push_queue_and_record_contention():
    _seed([("ads_order", "2026-11-01", "2026-11-01T18:00:00", "SL-1")])
    c = TestClient(app)
    h = _login(c)

    holding = threading.Event()
    release = threading.Event()

    def hold_gate():
        with appmod.WM_READ_GATE:
            holding.set()
            release.wait(5)

    t = threading.Thread(target=hold_gate)
    t.start()
    assert holding.wait(5)

    result = {}

    def history_call():
        result["hist"] = c.get("/api/watermarks/history", headers=h,
                               params={"consumer": "ads_order", "limit": 10})

    th = threading.Thread(target=history_call)
    th.start()
    time.sleep(0.25)  # 让历史读先排队在闸外
    release.set()
    th.join(10)
    t.join(5)

    assert result["hist"].status_code == 200
    contentions = _audit_rows("read_contention")
    assert contentions, "后手排队必须记下读争用"
    assert contentions[0]["code"] == "读争用"
    assert contentions[0]["read_mode"] == "history"
    assert contentions[0]["waited_ms"] >= 0

    audit = c.get("/api/watermarks/read-audit", headers=h).json()
    assert audit["contention_count"] >= 1


def test_release_pushes_current_and_invalidates_only_its_cache():
    c = TestClient(app)
    h = _login(c)
    # 先读一次，制造现行缓存条目。
    before = c.get("/api/watermarks/current", headers=h, params={"consumer": "ads_order"})
    assert before.json()["cache_key"] == "wm:current:ads_order"
    # 另一消费方的历史缓存不应被推现行波及。
    c.get("/api/watermarks/history", headers=h, params={"consumer": "other", "limit": 10})

    ok = c.post("/api/slices", headers=h, json={
        "slice_no": "SL-WM-1", "biz_date": "2026-11-05", "partition_key": "east",
        "row_count": 10, "expected_count": 10})
    sid = ok.json()["id"]
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_order"})
    assert rel.status_code == 200
    assert rel.json()["read_mode"] == "push"

    after = c.get("/api/watermarks/current", headers=h, params={"consumer": "ads_order"}).json()
    assert after["state"] == "ok"
    assert after["item"]["source_slice"] == "SL-WM-1"
    assert after["cache_hit"] is False

    cache = c.get("/api/watermarks/read-cache", headers=h).json()
    assert all(k.startswith("wm:current:") for k in cache["current_keys"])
    assert all(k.startswith("wm:history:") for k in cache["history_keys"])


def test_local_db_and_password_system_unchanged():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "日切放行"
    bad = c.post("/api/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401
    h = _login(c)
    assert c.get("/api/watermarks/current").status_code == 401
    assert c.get("/api/watermarks/current", headers=h).status_code == 200
    # 旧账本兼容接口仍可读，但不参与读写隔离。
    legacy = c.get("/api/watermarks", headers=h).json()
    assert legacy["read_mode"] == "legacy"
