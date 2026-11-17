人才驿站与服务窗口需求预测容量计划台
测试：python3 -m unittest discover -s tests -v
构建：python3 -m compileall -q service_capacity tests
验收：python3 -m service_capacity --database /tmp/service_capacity.sqlite3 run --fixture fixtures/acceptance.json --output /tmp/service_capacity-output.json
单测：python3 -m unittest tests.test_domain -v
容量计划：python3 -m service_capacity --database /tmp/service_capacity.sqlite3 plan --fixture fixtures/planning.json --output /tmp/service_capacity-plan.json

预测校准与容量决策（截至决策日可见数据）：
- 观测的 date 是业务发生日，recorded_at/known_at 是机构可见时间；滚动回测只用原点当时可见的记录。
- 决策日之后才补录的记录不进入训练；缺失日期只报告为缺口，不用前值填充冒充已知观测。
- 回测按站点和服务类型给出 MAE/RMSE/偏差/校准系数与各自样本量；样本不足时 status=insufficient_history。
- 容量计划把缺口分为 capacity_shortage（低端预测仍缺人）与 forecast_uncertainty（低端够、高端不够），
  并保留原服务时长、班次时长与占用率约束。
- 样本不足时不给出确定性人手建议（recommended_agents=null），只保留 indicative_agents 供排查。
- 调整排班后可与原方案比较：change=improved/worsened，并保留约束一致性检查。
- 每次校准与计划写入可追溯台账（决策日、知识截止时间、可见数据指纹、被排除的迟录记录、误差与校准系数）。
