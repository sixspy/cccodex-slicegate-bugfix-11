
import hashlib, secrets, sqlite3, threading, time
from contextlib import contextmanager
from pathlib import Path
from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 水位读法说明（钉住：甲）：
#   现行与历史分接口、分缓存键；历史翻页禁止写入现行缓存键。
#   任一接口不得混申读；偏离即拒绝并记“水位读口径偏了”。
WM_OPTION = "甲"
WM_DOC = "水位读法说明.md"
WM_DEVIATION_CODE = "水位读口径偏了"
WM_CONTENTION_CODE = "读争用"
WM_CURRENT_PREFIX = "wm:current:"
WM_HISTORY_PREFIX = "wm:history:"

# 水位读闸：现行读、历史翻页、放行推现行共用；后到者排队并记“读争用”。
WM_READ_GATE = threading.RLock()
# 缓存条目：(账本版本, 响应体)。现行与历史分别落在两套键空间，互不清写。
WM_CURRENT_CACHE: dict[str, tuple[tuple, dict]] = {}
WM_HISTORY_CACHE: dict[str, tuple[tuple, dict]] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from typing import Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    # 换库/重建库时清空两套读缓存键，避免把上一本账的现行读结果带过来。
    WM_CURRENT_CACHE.clear()
    WM_HISTORY_CACHE.clear()
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermark_read_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event TEXT NOT NULL,
          code TEXT NOT NULL,
          read_mode TEXT NOT NULL,
          consumer TEXT NOT NULL,
          detail TEXT NOT NULL,
          waited_ms INTEGER NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
            ]
            for r in rows:
                conn.execute("INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00')")
            conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west')")

@app.on_event("startup")
def _start():
    init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
    return {"items": [dict(r) for r in rows]}

class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int

@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    comp = "完整" if body.row_count >= body.expected_count and body.expected_count > 0 else ("部分完整" if body.row_count > 0 else "待检")
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)",
                (body.slice_no, body.biz_date, body.partition_key, body.row_count, body.expected_count, comp, "未放行"),
            )
            return {"id": cur.lastrowid, "completeness": comp}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "切片号已存在")

@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        comp = "完整" if row["row_count"] >= row["expected_count"] and row["expected_count"] > 0 else ("部分完整" if row["row_count"] > 0 else "待检")
        c.execute("UPDATE slices SET completeness=? WHERE id=?", (comp, sid))
    return {"id": sid, "completeness": comp}

class ReleaseIn(BaseModel):
    consumer: str

@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with wm_read_gate("push", body.consumer):
        with connect() as c:
            row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
            if not row:
                raise HTTPException(404, "切片不存在")
            if row["completeness"] != "完整":
                c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                          (sid, body.consumer, "拒绝", "完整度未达标", _now()))
                c.commit()
                raise HTTPException(409, "未完整禁止放行")
            c.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
            c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                      (sid, body.consumer, "成功", "完整切片放行", _now()))
            c.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
                      (body.consumer, row["biz_date"], _now(), row["slice_no"]))
        # 推现行只失效该消费方的现行键与历史键，两套键空间互不清写。
        _invalidate_wm_cache(body.consumer)
    return {"ok": True, "read_mode": "push", "pinned_option": WM_OPTION}

@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}

def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")

@contextmanager
def wm_read_gate(read_mode: str, consumer: str):
    """水位读闸：现行读/历史翻页/放行推现行排队；后到者记“读争用”。"""
    t0 = time.perf_counter()
    free = WM_READ_GATE.acquire(blocking=False)
    try:
        if not free:
            WM_READ_GATE.acquire()
            waited = int((time.perf_counter() - t0) * 1000)
            with connect() as c:
                _audit(c, "read_contention", WM_CONTENTION_CODE, read_mode, consumer,
                       f"后手排队 {waited}ms 后取得水位读闸", waited)
        yield
    finally:
        WM_READ_GATE.release()

def _audit(conn, event: str, code: str, read_mode: str, consumer: str, detail: str, waited_ms: int = 0):
    conn.execute(
        "INSERT INTO watermark_read_audit(event,code,read_mode,consumer,detail,waited_ms,created_at) VALUES (?,?,?,?,?,?,?)",
        (event, code, read_mode, consumer or "*", detail, waited_ms, _now()),
    )

def _deviation(read_mode: str, detail: str, consumer: str = ""):
    with connect() as c:
        _audit(c, "deviation", WM_DEVIATION_CODE, read_mode, consumer, detail)
    return JSONResponse(
        status_code=400,
        content={
            "code": WM_DEVIATION_CODE,
            "message": f"偏离水位读法说明（{WM_OPTION}）：现行与历史必须分接口、分缓存键，禁止混申读",
            "detail": detail,
            "read_mode": read_mode,
            "pinned_option": WM_OPTION,
            "doc": WM_DOC,
        },
    )

def _wm_version(conn, consumer: str | None):
    """账本版本 = (库路径, 条数, 最大 id)；换库或新推现行都会自然失效。"""
    if consumer:
        r = conn.execute("SELECT COUNT(*) c, COALESCE(MAX(id),0) m FROM watermarks WHERE consumer=?", (consumer,)).fetchone()
    else:
        r = conn.execute("SELECT COUNT(*) c, COALESCE(MAX(id),0) m FROM watermarks").fetchone()
    return (str(DB_PATH), r["c"], r["m"])

def _current_key(consumer: str | None) -> str:
    return f"{WM_CURRENT_PREFIX}{consumer or '*'}"

def _history_key(consumer: str | None, limit: int, cursor: int | None) -> str:
    return f"{WM_HISTORY_PREFIX}{consumer or '*'}|limit={limit}|cursor={cursor if cursor is not None else '-'}"

def _invalidate_wm_cache(consumer: str):
    for k in list(WM_CURRENT_CACHE):
        if k == _current_key(consumer) or k == _current_key(None):
            WM_CURRENT_CACHE.pop(k, None)
    for k in list(WM_HISTORY_CACHE):
        if k.startswith(f"{WM_HISTORY_PREFIX}{consumer}|") or k.startswith(f"{WM_HISTORY_PREFIX}*|"):
            WM_HISTORY_CACHE.pop(k, None)

def _current_payload(conn, consumer: str | None) -> dict:
    if consumer:
        rows = conn.execute("SELECT * FROM watermarks WHERE consumer=? ORDER BY id DESC LIMIT 1", (consumer,)).fetchall()
    else:
        rows = conn.execute("""SELECT w.* FROM watermarks w JOIN (
            SELECT consumer, MAX(id) mid FROM watermarks GROUP BY consumer) t ON t.mid=w.id
            ORDER BY w.id DESC""").fetchall()
    items = [dict(r) | {"row_role": "current"} for r in rows]
    payload = {
        "read_mode": "current",
        "pinned_option": WM_OPTION,
        "doc": WM_DOC,
        "consumer": consumer,
        "state": "ok",
        "reason_code": None,
        "reason": None,
        "count": len(items),
        "item": items[0] if (consumer and items) else None,
        "items": items,
    }
    if not items:
        total = conn.execute("SELECT COUNT(*) c FROM watermarks").fetchone()["c"]
        payload["state"] = "empty"
        payload["reason_code"] = "no_current_watermark"
        payload["reason"] = (f"消费方 {consumer} 尚无已放行水位；现行读不静默空白，也不拿历史首条冒充"
                             if consumer else "库中尚无任何已放行水位；请先放行完整切片")
        payload["ledger_rows"] = total
    return payload

def _history_payload(conn, consumer: str | None, limit: int, cursor: int | None) -> dict:
    # 历史页一律排除各消费方的现行条目，从结构上禁止“历史首条冒充现行”。
    if consumer:
        cur = conn.execute("SELECT MAX(id) m FROM watermarks WHERE consumer=?", (consumer,)).fetchone()["m"]
        excluded = [cur] if cur else []
    else:
        excluded = [r["m"] for r in conn.execute("SELECT MAX(id) m FROM watermarks GROUP BY consumer")]
    marks = ",".join("?" * len(excluded))
    where = f"id NOT IN ({marks})" if excluded else "1=1"
    params: list = list(excluded)
    if consumer:
        where += " AND consumer=?"
        params.append(consumer)
    if cursor is not None:
        where += " AND id < ?"
        params.append(cursor)
    total = conn.execute("SELECT COUNT(*) c FROM watermarks").fetchone()["c"]
    rows = conn.execute(f"SELECT * FROM watermarks WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit + 1)).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    items = [dict(r) | {"row_role": "history"} for r in page]
    payload = {
        "read_mode": "history",
        "pinned_option": WM_OPTION,
        "doc": WM_DOC,
        "consumer": consumer,
        "state": "ok",
        "reason_code": None,
        "reason": None,
        "limit": limit,
        "cursor": cursor,
        "next_cursor": items[-1]["id"] if (has_more and items) else None,
        "has_more": has_more,
        "excluded_current_id": excluded[0] if (consumer and excluded) else None,
        "count": len(items),
        "items": items,
    }
    if not items:
        payload["state"] = "empty"
        own = conn.execute("SELECT COUNT(*) c FROM watermarks WHERE consumer=?",
                           (consumer,)).fetchone()["c"] if consumer else total
        if total == 0:
            payload["reason_code"] = "no_history"
            payload["reason"] = "库中暂无历史水位；现行条目不计入历史页"
        elif own == 0:
            payload["reason_code"] = "no_current_watermark"
            payload["reason"] = f"消费方 {consumer} 尚无任何水位；历史页与现行页均无数据，不静默空白"
        else:
            payload["reason_code"] = "no_history_only_current"
            payload["reason"] = "该消费方暂无历史水位，仅现行一条；现行请单独读取 /api/watermarks/current"
    return payload

CURRENT_FORBIDDEN = {"limit", "cursor", "page", "offset", "include_history", "history"}
HISTORY_FORBIDDEN = {"include_current", "current_only"}

def _read_mode_deviates(request: Request, read_mode: str) -> str | None:
    q = request.query_params
    forbidden = CURRENT_FORBIDDEN if read_mode == "current" else HISTORY_FORBIDDEN
    hit = sorted(forbidden & set(q.keys()))
    if hit:
        return f"{read_mode} 读接口收到跨侧参数: {', '.join(hit)}"
    declared = q.get("read_mode")
    if declared and declared != read_mode:
        return f"{read_mode} 读接口收到 read_mode={declared}"
    header = request.headers.get("X-Watermark-Read-Mode")
    if header and header != "current" and header != "history":
        return f"未知读模式请求头: {header}"
    if header and header != read_mode:
        return f"{read_mode} 读接口收到跨侧读模式请求头: {header}"
    return None

@app.get("/api/watermarks/current")
def wm_current(request: Request, consumer: str | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    bad = _read_mode_deviates(request, "current")
    if bad:
        return _deviation("current", bad, consumer or "")
    with wm_read_gate("current", consumer or ""):
        key = _current_key(consumer)
        with connect() as c:
            version = _wm_version(c, consumer)
            cached = WM_CURRENT_CACHE.get(key)
            if cached and cached[0] == version:
                return dict(cached[1]) | {"cache_key": key, "cache_hit": True}
            payload = _current_payload(c, consumer)
        WM_CURRENT_CACHE[key] = (version, payload)
        return dict(payload) | {"cache_key": key, "cache_hit": False}

@app.get("/api/watermarks/history")
def wm_history(request: Request, consumer: str | None = None, limit: int = 20, cursor: int | None = None,
               authorization: str | None = Header(default=None)):
    require_user(authorization)
    bad = _read_mode_deviates(request, "history")
    if bad:
        return _deviation("history", bad, consumer or "")
    if limit < 1 or limit > 100:
        raise HTTPException(400, "limit 须在 1..100")
    with wm_read_gate("history", consumer or ""):
        key = _history_key(consumer, limit, cursor)
        with connect() as c:
            version = _wm_version(c, consumer)
            cached = WM_HISTORY_CACHE.get(key)
            if cached and cached[0] == version:
                return dict(cached[1]) | {"cache_key": key, "cache_hit": True}
            payload = _history_payload(c, consumer, limit, cursor)
        # 历史读只写历史键空间；显式不触碰 WM_CURRENT_CACHE。
        WM_HISTORY_CACHE[key] = (version, payload)
        return dict(payload) | {"cache_key": key, "cache_hit": False}

@app.get("/api/watermarks/read-policy")
def wm_read_policy(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        contention = c.execute("SELECT COUNT(*) n FROM watermark_read_audit WHERE event='read_contention'").fetchone()["n"]
        deviation = c.execute("SELECT COUNT(*) n FROM watermark_read_audit WHERE event='deviation'").fetchone()["n"]
    return {
        "pinned_option": WM_OPTION,
        "doc": WM_DOC,
        "rule": "现行与历史分接口、分缓存键；历史翻页禁止写入现行缓存键",
        "current_endpoint": "/api/watermarks/current",
        "history_endpoint": "/api/watermarks/history",
        "cache_keys": {"current": f"{WM_CURRENT_PREFIX}{{consumer}}", "history": f"{WM_HISTORY_PREFIX}{{consumer}}|limit={{limit}}|cursor={{cursor|-}}"},
        "empty_state_required": True,
        "deviation_code": WM_DEVIATION_CODE,
        "contention_code": WM_CONTENTION_CODE,
        "counts": {"contention": contention, "deviation": deviation},
        "five_places": [
            {"place": "watermark-page", "marker": "data-wm-place=\"watermark-page\""},
            {"place": "history-detail", "marker": "data-wm-place=\"history-detail\""},
            {"place": "release-flow", "marker": "data-wm-place=\"release-flow\""},
            {"place": "list-side-note", "marker": "data-wm-place=\"list-side-note\""},
            {"place": "policy-bar", "marker": "data-wm-place=\"policy-bar\""},
        ],
    }

@app.get("/api/watermarks/read-audit")
def wm_read_audit(limit: int = 50, authorization: str | None = Header(default=None)):
    require_user(authorization)
    limit = max(1, min(limit, 200))
    with connect() as c:
        rows = c.execute("SELECT * FROM watermark_read_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        contention = c.execute("SELECT COUNT(*) n FROM watermark_read_audit WHERE event='read_contention'").fetchone()["n"]
        deviation = c.execute("SELECT COUNT(*) n FROM watermark_read_audit WHERE event='deviation'").fetchone()["n"]
    return {"items": [dict(r) for r in rows], "contention_count": contention, "deviation_count": deviation}

@app.get("/api/watermarks/read-cache")
def wm_read_cache(authorization: str | None = Header(default=None)):
    require_user(authorization)
    return {
        "pinned_option": WM_OPTION,
        "current_keys": sorted(WM_CURRENT_CACHE),
        "history_keys": sorted(WM_HISTORY_CACHE),
        "isolated": all(k.startswith(WM_CURRENT_PREFIX) for k in WM_CURRENT_CACHE)
                    and all(k.startswith(WM_HISTORY_PREFIX) for k in WM_HISTORY_CACHE),
    }

@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    # 兼容旧调用：只读原始账本，不写任何读缓存键，不参与现行/历史读隔离。
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows], "read_mode": "legacy", "doc": WM_DOC}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")

app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
