人才驿站与服务窗口需求预测容量计划台
测试：python3 -m unittest discover -s tests -v
构建：python3 -m compileall -q service_capacity tests
验收：python3 -m service_capacity --database /tmp/service_capacity.sqlite3 run --fixture fixtures/acceptance.json --output /tmp/service_capacity-output.json
单测：python3 -m unittest tests.test_domain -v
