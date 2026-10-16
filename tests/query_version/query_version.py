from pathlib import Path
import yaml
from utility_command import UtilityCommand


class versionTest:
    def run(self, run_id: str) -> None:
        test_path = Path(__file__).resolve()
        with test_path.with_name("config.yaml").open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        UtilityCommand.run(
            run_id=run_id,
            test_name="QueryVersionTest",
            port=str(config["port"]),
            target=str(config["target"]),
            command="version",
        )