# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 物资交接清单流程

针对夜班交接"只能证明谁操作过、不能证明双方确认的是同一组物资"的问题，新增交接清单（`wh_handoff_*` 表）：

1. **创建（draft）**：移交人提交清单（物资、数量、封签号、存放位置）与接收人；支持 `idempotency_key`，重复提交/服务重试只返回原单，不会产生第二次交接。
2. **冻结（frozen）**：移交人冻结清单内容并快照物资名称/编码；冻结时校验任一物资是否被其他业务（其他在途交接、待审批/已通过出库）占用，被占用则整单不得冻结。
3. **逐项确认**：接收人逐项核对并回填实收数量、实查封签、实查位置；三项与快照一致记为 confirmed，任一不一致记为 disputed，disputed 项必须在最终生效前处理。
4. **退回修订（returned）**：确认完成前接收人可注明差异原因退回；移交人修订后重新冻结，版本号 +1，原确认结果作废。
5. **完成（completed）**：全部明细 confirmed 且生效前复检无占用，才完成责任转移，`custodian` 由移交人变更为接收人。
6. **更正（correction）**：已完成清单不可修改，只能由主管追加更正记录，版本递增并记录新旧责任人和原因。

主管可通过 `GET /api/handoffs/` 与 `GET /api/handoffs/<id>/` 查询每一次交接的版本、双方身份、逐项差异原因、事件流水和最终责任归属。

接口一览：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/handoffs/` | 移交人创建清单（幂等） |
| GET | `/api/handoffs/` | 交接单列表（支持 status/handoff_no/goods_code 过滤） |
| GET | `/api/handoffs/<id>/` | 详情：版本、双方、差异、事件、更正、责任归属 |
| PUT | `/api/handoffs/<id>/` | 待冻结/退回状态下修订清单 |
| POST | `/api/handoffs/<id>/freeze/` | 移交人冻结 |
| POST | `/api/handoffs/<id>/items/<item_id>/confirm/` | 接收人逐项确认数量/封签/位置 |
| POST | `/api/handoffs/<id>/return/` | 接收人退回修订（需填原因） |
| POST | `/api/handoffs/<id>/complete/` | 接收人完成责任转移 |
| POST | `/api/handoffs/<id>/corrections/` | 主管更正已完成清单 |

状态机：`draft → frozen ⇄ returned → completed`（completed 只能经 corrections 更正）。

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
