#!/usr/bin/env python3
"""Run the complete site twice on an already converged lab and compare snapshots.

Run on the Ansible controller. Pass inventory/extra-vars after --. Evidence is
outside Git; this helper runs real playbooks, not --check mode.
"""
import argparse
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess


RECAP = re.compile(r'^\s*(\S+)\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)\s+skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)', re.M)


def assert_recap(text):
    section = text.rsplit('PLAY RECAP', 1)
    rows = RECAP.findall(section[-1]) if len(section) == 2 else []
    if not rows:
        raise RuntimeError('Missing default-callback PLAY RECAP')
    for host, ok, changed, unreachable, failed, skipped, rescued, ignored in rows:
        if any(int(v) for v in (changed, unreachable, failed, rescued, ignored)):
            raise RuntimeError('Nonzero changes/errors in recap for ' + host)
    return {row[0]: {'ok': int(row[1]), 'changed': int(row[2])} for row in rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('ansible_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = args.output_dir.resolve()
    if out == root or root in out.parents:
        parser.error('Evidence directory must be outside the source repository')
    extra = args.ansible_args
    if extra[:1] == ['--']:
        extra = extra[1:]
    if any(a in ('--check', '-C', '--tags', '-t', '--skip-tags', '--start-at-task', '--limit', '-l')
           or a.startswith(('--tags=', '--skip-tags=', '--start-at-task=', '--limit=')) for a in extra):
        parser.error('This acceptance check must run the complete site, without limits/tags/check mode')
    out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, ANSIBLE_STDOUT_CALLBACK='default', ANSIBLE_NOCOLOR='1', ANSIBLE_FORCE_COLOR='0')
    result = {'status': 'FAIL', 'started_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'Full site recap plus selected live object identities; not proof of zero API writes.',
              'runs': []}

    def playbook(name, logname, more=None):
        cmd = ['ansible-playbook', str(root / name)] + extra + (more or [])
        print('Running', name, 'log:', out / logname, flush=True)
        with (out / logname).open('w') as log:
            completed = subprocess.run(cmd, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        if completed.returncode:
            raise RuntimeError(name + ' failed; inspect ' + logname)
        return (out / logname).read_text()

    def snapshot(name):
        # Structured extra-vars avoids shell interpolation and preserves spaces.
        playbook('playbooks/snapshot.yml', name + '.log',
                 ['-e', json.dumps({'snapshot_output': str(out / (name + '.json'))})])
        return json.loads((out / (name + '.json')).read_text())

    try:
        result['source_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        result['source_dirty'] = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True).strip())
        source_paths = subprocess.check_output(
            ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z',
             'site.yml', 'playbooks', 'vars', 'scripts', 'charts', 'templates',
             'requirements.yml', 'ansible.cfg', 'Vagrantfile', 'inventory*.ini'], cwd=root).decode().split('\0')
        result['source_sha256'] = {p: hashlib.sha256((root / p).read_bytes()).hexdigest()
                                   for p in sorted(set(source_paths)) if p and (root / p).is_file()}

        # Record the exact working-tree patch separately if testing uncommitted fixes.
        (out / 'source.patch').write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=root))
        previous = snapshot('before')
        for index in (1, 2):
            text = playbook('site.yml', 'site-' + str(index) + '.log')
            recap = assert_recap(text)
            current = snapshot('after-' + str(index))
            same = current == previous
            result['runs'].append({'run': index, 'recap': recap, 'snapshot_unchanged': same})
            if not same:
                raise RuntimeError('Live identity/state changed; compare before/after snapshot JSON')
            previous = current
        result['status'] = 'PASS'
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        result['finished_utc'] = datetime.now(timezone.utc).isoformat()
        (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
