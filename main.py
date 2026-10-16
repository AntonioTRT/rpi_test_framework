from uuid import uuid4

from tests.query_version.query_version import  versionTest
from tests.batteryStatusLoop.batteryStatusLoop import LoopTest
run_id = str(uuid4())

LoopTest().run(run_id)