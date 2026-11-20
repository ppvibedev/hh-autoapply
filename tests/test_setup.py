"""Offline installer checks; fake interpreters never install or access the network."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class SetupTests(unittest.TestCase):
    def test_supported_python_and_missing_or_wrong_version(self):
        source = Path(__file__).resolve().parents[1]
        for valid in (True, False):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                shutil.copy(source / 'setup.sh', root)
                (root / '.env.example').write_text('RUN_MODE=SAFE\n')
                (root / '.env').write_text('KEEP_LOCAL=1\n')
                for name in ('state', 'private'):
                    (root / name).mkdir()
                    (root / name / 'keep').write_text('synthetic')
                binpath = root / 'bin'; binpath.mkdir()
                for name in ('python3.11', 'python3'):
                    command = binpath / name
                    command.write_text('#!/bin/bash\nif [ "$1" = "-c" ]; then exit ' +
                                       ('0' if valid else '1') + '; fi\n'
                                       'if [ "$1" = "-m" ]; then\n'
                                       ' /bin/mkdir -p .venv/bin\n'
                                       ' /bin/cp "$0" .venv/bin/python\n'
                                       ' for tool in pip playwright; do\n'
                                       '  /bin/echo "#!/bin/bash" > .venv/bin/$tool\n'
                                       '  /bin/echo "exit 0" >> .venv/bin/$tool\n'
                                       '  /bin/chmod +x .venv/bin/$tool\n'
                                       ' done\nfi\n')
                    command.chmod(0o755)
                env = dict(os.environ, PATH=str(binpath) + ':/usr/bin:/bin')
                result = subprocess.run(['/bin/bash', 'setup.sh'], cwd=root, env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if valid else 1, result.stdout + result.stderr)
                self.assertEqual((root / '.env').read_text(), 'KEEP_LOCAL=1\n')
                for name in ('state', 'private'):
                    self.assertEqual((root / name / 'keep').read_text(), 'synthetic')
                if not valid:
                    self.assertIn('Python 3.11', result.stdout)
                    self.assertFalse((root / '.venv').exists())
