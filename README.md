# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```

## 交接清单流程

交接清单用于解决“收发记录只能说明谁操作过、不能证明交接双方确认的是同一组物资”的问题。
责任转移以接收人逐项确认并最终确认为准。

### 状态流转

```
draft（编制中）
  └─移交人冻结─▶ submitted（待确认，版本号 +1，内容不可改）
                   ├─接收人逐项确认（数量/封签号/存放位置）全部一致─▶ completed（责任转移至接收人，终态）
                   ├─确认前退回（须填原因）─▶ returned ─移交人修订─▶ 重新冻结（新版本）
                   └─移交人作废─▶ cancelled
```

- 冻结后清单内容不可改动；接收人确认前可**退回修订**，每次重新冻结生成新版本，历史版本快照保留。
- 接收人必须对当前版本的**每一项**逐项确认；任一项标记“有差异”则整单不能完成，需退回修订。
- 冻结与完成时都会校验占用：任一物资已被**已审批出库单预留**或被**其他冻结中的交接单占用**时，整单不得生效（HTTP 409）。
- `completed` 为终态，不能再冻结/退回/作废/改行项；后续变动只能通过**更正记录**追加处理（`/corrections/`）。
- 创建与完成支持 `Idempotency-Key` 请求头或 `idempotency_key` 字段；幂等记录与交接单同事务提交，服务重启或重复提交不会制造第二次交接。

### 主要接口（均在 `/api` 前缀下，需登录）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/handovers/` | 移交人创建清单（可带幂等键） |
| GET | `/handovers/` | 清单查询（主管可查全部，普通用户仅查与己相关） |
| GET | `/handovers/<id>/` | 详情：版本、双方身份、差异原因、最终责任归属、事件、更正 |
| PUT | `/handovers/<id>/items/` | 确认前（编制中/已退回）修订物资行 |
| POST | `/handovers/<id>/freeze/` | 移交人冻结清单 |
| POST | `/handovers/<id>/items/<item_id>/adjudicate/` | 接收人逐项确认 |
| POST | `/handovers/<id>/return/` | 接收人退回修订（须填 `reason`） |
| POST | `/handovers/<id>/complete/` | 接收人完成交接、责任转移（可带幂等键） |
| POST | `/handovers/<id>/cancel/` | 移交人完成前作废 |
| GET | `/handovers/<id>/versions/` | 各冻结版本快照 |
| GET | `/handovers/<id>/events/` | 状态流转与差异原因事件时间线 |
| GET/POST | `/handovers/<id>/corrections/` | 已完成清单的更正记录（只追加） |
| GET | `/handovers/occupancy-check/?goods=1,2` | 冻结前占用预检 |

