import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class IsolationTest(unittest.TestCase):
    def test_hidden_tests_and_upstream_history_unavailable_until_agent_finishes(self):
        with tempfile.TemporaryDirectory(prefix='tb-isolation-') as tmp:
            base = Path(tmp).resolve()
            runner = base / 'runner' / 'tb'
            runner.mkdir(parents=True)
            for name in ('tb_run_one.py', 'port.py'):
                shutil.copy2(Path(__file__).parents[1] / name, runner / name)
            upstream = base / 'upstream'
            task = upstream / 'original-tasks' / 'sample'
            (task / 'tests').mkdir(parents=True)
            (upstream / '.git').mkdir()
            (upstream / '.git' / 'answer').write_text('private reference')
            (task / 'Dockerfile').write_text('FROM ghcr.io/laude-institute/t-bench/python-3-13:20250620\n')
            (task / 'task.yaml').write_text('instruction: |\n  Write answer.txt containing done.\n')
            (task / 'tests' / 'test_outputs.py').write_text(
                'from pathlib import Path\ndef test_answer():\n'
                '    assert Path("answer.txt").read_text() == "done"\n'
                '    assert Path("/tests/test_outputs.py").exists()\n')
            dsh = base / 'dsh'
            dsh.mkdir()
            (dsh / 'drive_dsh.py').write_text(
                'from pathlib import Path\nimport sys\n'
                'cwd=Path(sys.argv[1])\n'
                'assert cwd.name == "app"\n'
                'assert not (cwd.parent / "tests").exists()\n'
                f'assert not Path({str(upstream / "original-tasks")!r}).exists()\n'
                f'assert not Path({str(upstream / ".git")!r}).exists()\n'
                '(cwd / "answer.txt").write_text("done")\n')
            result = subprocess.run([
                sys.executable, str(runner / 'tb_run_one.py'), '--tb', str(task.parent),
                '--task', 'sample', '--arm', 'agent', '--root', str(base / 'root'),
            ], env=dict(os.environ, DSH_DIR=str(dsh), ENVSHIFT_API_KEY='test-placeholder'),
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('passed=1/1', result.stdout, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
