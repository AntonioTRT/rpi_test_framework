from uuid import uuid4

from tests.query_version.query_version import  versionTest
from tests.command_list.command_list import LoopTest
run_id = str(uuid4())

LoopTest().run(run_id)