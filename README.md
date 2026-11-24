# 光伏电站限电事件与损失电量复算

给新能源场站运营分析员使用。仅用有效对照逆变器和相同辐照区间拟合基线；重叠限电事件先求并集再积分。光储共用并网点时，电池充放量永不计入限发，每段由光伏发电、电池充放、上网功率三方互证，解释不了的片段留待查。

八个业务模块：遥测采信、设备映射、事件切片、基线拟合、损失核算、能量对账、储能仲裁、运行归档。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

访问 http://localhost:18244 。启动直接载入现场脏批。切换干净样例和旧编码样例可对照核验；「光储共用并网点」样例含六段场景：正常、充电压低上网（净差法误算 6.6667kWh 但不可申报）、放电补回（约束缺口 5kWh 可申报，净差法反算为 0）、两段账差待查、一段表计缺测待查。完整对照批INV-A重叠事件取并集后30分钟，基线100kW、实际60kW，损失20kWh；无合格对照或遥测冲突必须保持未定，不能填0。

各模块页面对应 `/<模块英文名>`，各模块执行入口为 `POST /api/<模块英文名>`，不传参即可独立演示。完整运行 `POST /api/runs` 支持 `dataset`（dirty/clean/legacy/storage）、`csv_text` 与 `overrides` 参数。批次历史 `GET /api/runs`，详情 `GET /api/runs/{run_id}`。

上游充放/限值依据走 `GET|POST /api/runs/{run_id}/basis`，字段限 `charge_kw`、`discharge_kw`、`export_limit_kw`。同一依据重试幂等不新增电量；校验失败事务回滚、旧版依据与现场凭据保留；切换后只重算受影响片段，片段标注依据版本（首跑 r0，首次上游依据 r1）。储能仲裁页展示可申报损失、电池影响（净差法误算对照）和待查片段，导出包新增 `storage_adjudication.json`。

同输入与规则版本生成相同批号，只保留一条批次。复核入口 `POST /api/runs/{run_id}/review` 提交当前 `revision`，陈旧提交409。归档包只有一个下载入口 `GET /api/export/{run_id}`，含证据和运行摘要。原始隔离在页面同屏显示。

SQLite使用命名卷，容器重启后保留历史。要重建演示可在Compose中改用新的卷名称，保留旧卷。

源码在 `app/`，样例在 `sample_data/`，测试在 `tests/`；定义见 `docs/DATA-DICTIONARY.md`，模块和边界测试对应关系见 `docs/DEPTH.md`。输入上限2MB，属于离线复核工具。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。
