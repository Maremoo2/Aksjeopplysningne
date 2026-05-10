from __future__ import annotations

import unittest
from pathlib import Path


class WorkflowConfigTests(unittest.TestCase):
    def test_momentum_workflow_wires_usa_data_provider_input(self) -> None:
        workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "momentum-screener.yml"
        content = workflow_path.read_text(encoding="utf-8")

        self.assertIn("usa_data_provider:", content)
        self.assertIn("USA_DATA_PROVIDER: ${{ github.event.inputs.usa_data_provider || 'yahoo' }}", content)
        self.assertIn("--usa-data-provider \"$USA_DATA_PROVIDER\"", content)
        self.assertIn("--nordic-universe \"$NORDIC_UNIVERSE\"", content)


if __name__ == "__main__":
    unittest.main()
