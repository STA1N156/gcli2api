import unittest
from pathlib import Path


class DockerWorkflowTagTests(unittest.TestCase):
    def test_sha_tag_prefix_is_valid_when_branch_context_is_missing(self):
        workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

        self.assertNotIn("type=sha,prefix={{branch}}-", workflow)
        self.assertIn("type=sha,prefix=sha-", workflow)


if __name__ == "__main__":
    unittest.main()
