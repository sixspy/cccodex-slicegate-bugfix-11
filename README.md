# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10636 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 水位读法（钉住：甲）

现行水位与历史水位**分接口、分缓存键**，历史翻页禁止写入现行缓存键；缺现行必须落显式空态原因，禁止静默空白，也禁止拿历史首条冒充现行。完整口径、并发排队（读争用）、五处对齐与验收要求见 [水位读法说明.md](水位读法说明.md)。

| 用途 | 接口 |
|------|------|
| 现行水位 | `GET /api/watermarks/current?consumer=<消费方>` |
| 历史水位（分页） | `GET /api/watermarks/history?consumer=<消费方>&limit=&cursor=` |
| 读策略 / 读审计 / 读缓存 | `GET /api/watermarks/read-policy` · `/read-audit` · `/read-cache` |

## 启动（一键）

`powershell
cd projects/04-slicegate
docker compose up -d --build
`

浏览器：http://127.0.0.1:10636/  
健康检查：GET http://127.0.0.1:10636/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10601
python -m pytest -q
`
