"""daily_pipeline.sh must base runs on origin/main even when another worktree holds `main` (2026-11-20, PR #115)."""
import os, re, subprocess, tempfile, unittest

SCRIPT = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'daily_pipeline.sh')


def sh(cmd, cwd):
    return subprocess.run(['bash', '-c', cmd], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class DetachedMainTest(unittest.TestCase):
    def test_no_local_main_checkout(self):
        src = open(SCRIPT).read()
        self.assertEqual(src.count('git checkout -q main'), 1)  # only inside to_main
        self.assertIn('to_main ||', src)

    def test_to_main_works_when_main_held_by_other_worktree(self):
        fn = re.search(r'^to_main\(\) \{\n.*?\n\}$', open(SCRIPT).read(), re.M | re.S).group(0)
        with tempfile.TemporaryDirectory() as d:
            g = 'git -c user.email=t@t -c user.name=t'
            sh(f'git init -q -b main --bare origin.git && git clone -q origin.git home', d)
            home = os.path.join(d, 'home')
            sh(f'{g} commit -q --allow-empty -m base && git push -q origin main', home)
            sh(f'git worktree add -q -b stale ../wt && {g} commit -q --allow-empty -m new && git push -q origin main', home)
            wt = os.path.join(d, 'wt')
            self.assertNotEqual(sh('git rev-parse HEAD', wt), sh('git rev-parse origin/main', home))
            sh(fn + '\nto_main', wt)
            self.assertEqual(sh('git rev-parse HEAD', wt), sh('git rev-parse HEAD', home))


if __name__ == '__main__':
    unittest.main()
