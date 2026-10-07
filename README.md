# 生物样本库知情同意与撤回

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8302`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8302
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `participant`：参与者；`consent`：同意版本；`sample`：样本；`withdrawal`：撤回申请。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/batches`：批量入库（见下）。
- `GET /api/batches/<batch_id>`：查询批次及每条条目的核对结果。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 同意版本重算与受限样本

- 委员会激活新版同意（`consent/activate`）时，旧版同意自动变为`superseded`，旧版样本的可用范围按新`scope`重算：新范围未覆盖的用途写入`restricted_purposes`，样本`availability`变为`restricted`，并记录`restricted_basis`（触发重算的同意ID与版本，页面上显示为“受限依据版本”）。
- 受限样本的借出（`loan`，含用途必须在`granted_purposes`内）与匿名化（`anonymize`）被拦截，返回`409 RestrictedUseError`；在借样本可归还，销毁不受限。
- 参与者补签新同意（`consent/countersign`）后，受限样本重新挂到新同意版本，清除受限标记，借出与匿名化恢复。
- 激活重算与批量入库共用一个`BEGIN IMMEDIATE`事务。两者撞车时整批样本只落在同一个同意版本下：要么整批按旧版入库（随后被重算），要么整批被拒绝（`409`），不会跨版本拆分。

## 批量入库与续跑

`POST /api/batches`请求体：

```json
{"batch_id": "可选，重试时传同一个",
 "consent_id": "整批钉死的同意版本",
 "items": [{"ref": "调用方稳定键", "participant_id": "...", "sample_code": "...",
            "collected_at": "...", "freezer": "...", "position": "..."}]}
```

条目逐条核对：合法的直接入库，失败的写入批次明细且不影响其他条目，批次状态为`completed`或`partial`。用同一`batch_id`重试时，已算好的条目直接复用，只重提并核对之前失败的条目；若钉住的同意版本已被作废旧，重试整批拒绝（`409`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

样本销毁和外部机构调用是流程演示，不会自动删除真实存储中的样本。
