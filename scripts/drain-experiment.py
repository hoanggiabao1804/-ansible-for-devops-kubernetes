#!/usr/bin/env python3
"""Explicit lab experiment called by Ansible. No forced eviction or pod deletion.

Prove PDB blocking with a frontend-only drain, restore PDB, then drain the
requested scope. All-node drain can interrupt singleton backends and Redis.
"""
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError


def ready(pod):
    return (not pod['metadata'].get('deletionTimestamp') and
            any(c['type'] == 'Ready' and c['status'] == 'True'
                for c in pod.get('status', {}).get('conditions', [])))


def run(config):
    out = Path(config['output'])
    out.mkdir(parents=True, exist_ok=False)
    command = ['kubectl', '--kubeconfig', config['kubeconfig'], '--request-timeout=30s']
    namespace, node = config['namespace'], config['node']
    result = {'status': 'FAIL', 'scope': config['scope'], 'node': node,
              'availability_note': 'HTTP samples measure homepage only, not checkout availability.'}
    stop = threading.Event()
    monitor_thread = None
    saved_spec = None
    touched = False

    def kubectl(*args, check=True, timeout=45):
        p = subprocess.run(command + list(args), text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        if check and p.returncode:
            raise RuntimeError('kubectl ' + ' '.join(args) + ': ' + p.stderr)
        return p

    def get(kind, name=None, all_namespaces=False):
        args = ['get', kind] + ([name] if name else [])
        args += ['-A'] if all_namespaces else ['-n', namespace]
        return json.loads(kubectl(*args, '-o', 'json').stdout)

    def save(name, data):
        (out / name).write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')

    def snapshot(stage):
        save(stage + '.json', {kind: get(kind, all_namespaces=True)
                              for kind in ('pods', 'deployments', 'pdb', 'events')})
        (out / (stage + '-node.txt')).write_text(kubectl('describe', 'node', node).stdout)

    def wait_for(predicate, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(2)
        raise RuntimeError('Timed out waiting for the expected PDB or rollout state')

    def budget_matches(blocked):
        pdb = get('pdb', 'frontend')
        st = pdb.get('status', {})
        return (st.get('observedGeneration') == pdb['metadata']['generation'] and
                ((st.get('disruptionsAllowed') == 0) if blocked
                 else (st.get('disruptionsAllowed', 0) >= 1)))

    def restore_pdb():
        kubectl('-n', namespace, 'patch', 'pdb', 'frontend', '--type=json', '-p',
                json.dumps([{'op': 'replace', 'path': '/spec', 'value': saved_spec}]))

    def monitor():
        opener = build_opener(ProxyHandler({}))
        with (out / 'http-during-drain.jsonl').open('w') as f:
            while not stop.is_set():
                record = {'time_unix': time.time()}
                try:
                    req = Request(config['endpoint'] + '/', headers={'Host': config['host']})
                    try:
                        response = opener.open(req, timeout=5)
                    except HTTPError as exc:
                        response = exc
                    with response:
                        record['status'] = response.code
                        record['boutique_content'] = b'Online Boutique' in response.read(1024 * 1024)
                except Exception as exc:
                    record['error'] = str(exc)
                f.write(json.dumps(record) + '\n'); f.flush()
                stop.wait(1)

    try:
        if config['scope'] not in ('all', 'frontend'):
            raise RuntimeError('scope must be all or frontend')
        nodes = get('nodes')['items']
        target = next(x for x in nodes if x['metadata']['name'] == node)
        if node not in config['workers'] or target.get('spec', {}).get('unschedulable'):
            raise RuntimeError('Target must be an initially schedulable inventory worker')
        labels = target['metadata'].get('labels', {})
        if any(k in labels for k in ('node-role.kubernetes.io/control-plane', 'node-role.kubernetes.io/master')):
            raise RuntimeError('Refusing to drain a control plane')
        workers = [n for n in nodes if n['metadata']['name'] in config['workers']
                   and not n.get('spec', {}).get('unschedulable')
                   and any(c['type'] == 'Ready' and c['status'] == 'True'
                           for c in n['status'].get('conditions', []))]
        if len(workers) < 2 or node not in [n['metadata']['name'] for n in workers]:
            raise RuntimeError('Need two Ready schedulable workers; check surviving-node capacity')
        dep = get('deployment', 'frontend')
        replicas = dep['spec']['replicas']
        pods = [p for p in get('pods')['items'] if p['metadata'].get('labels', {}).get('app') == 'frontend']
        if replicas < 2 or len(pods) != replicas or not all(ready(p) for p in pods):
            raise RuntimeError('Need at least two stable Ready frontend replicas')
        if not any(p['spec']['nodeName'] == node for p in pods) or not any(p['spec']['nodeName'] != node for p in pods):
            raise RuntimeError('Frontend must already be distributed across target and another node')
        all_pods = get('pods', all_namespaces=True)['items']
        if any(p['metadata'].get('labels', {}).get('app') == 'frontend'
               and p['metadata']['namespace'] != namespace for p in all_pods):
            raise RuntimeError('Frontend selector also matches another namespace')
        if config['scope'] == 'all' and not config['delete_emptydir']:
            if any(p['spec'].get('nodeName') == node and any('emptyDir' in v for v in p['spec'].get('volumes', []))
                   and p['status'].get('phase') not in ('Succeeded', 'Failed') for p in all_pods):
                raise RuntimeError('Full drain encounters emptyDir; explicitly opt in to data loss or use frontend scope')
        pdb = get('pdb', 'frontend')
        saved_spec = pdb['spec']
        if saved_spec.get('selector', {}).get('matchLabels') != {'app': 'frontend'}:
            raise RuntimeError('Unexpected frontend PDB selector')
        save('original-pdb.json', pdb)
        snapshot('before')
        touched = True
        # Replace the entire spec so minAvailable/maxUnavailable never coexist.
        blocked_spec = {'selector': saved_spec['selector'], 'minAvailable': replicas}
        kubectl('-n', namespace, 'patch', 'pdb', 'frontend', '--type=json', '-p',
                json.dumps([{'op': 'replace', 'path': '/spec', 'value': blocked_spec}]))
        wait_for(lambda: budget_matches(True))
        blocked = kubectl('drain', node, '--ignore-daemonsets', '--pod-selector=app=frontend',
                          '--timeout=20s', check=False, timeout=60)
        (out / 'blocked-drain.log').write_text(blocked.stdout + blocked.stderr)
        after_pods = get('pods')['items']
        after_uids = {p['metadata']['uid'] for p in after_pods if ready(p)}
        if (blocked.returncode == 0 or 'disruption budget' not in (blocked.stdout + blocked.stderr).lower()
                or not {p['metadata']['uid'] for p in pods}.issubset(after_uids)):
            raise RuntimeError('Did not prove PDB rejection without replacing original frontend Pods')
        result['pdb_blocked_eviction'] = True
        snapshot('blocked')
        restore_pdb()
        wait_for(lambda: budget_matches(False))
        monitor_thread = threading.Thread(target=monitor, daemon=True); monitor_thread.start()
        args = ['drain', node, '--ignore-daemonsets', '--timeout=' + str(config['timeout']) + 's']
        if config['scope'] == 'frontend':
            args += ['--pod-selector=app=frontend']
        if config['delete_emptydir']:
            args += ['--delete-emptydir-data']
        drained = kubectl(*args, check=False, timeout=config['timeout'] + 45)
        (out / 'allowed-drain.log').write_text(drained.stdout + drained.stderr)
        snapshot('after-drain')
        if drained.returncode:
            raise RuntimeError('Drain failed; inspect log, PDBs and surviving-worker capacity')
        kubectl('-n', namespace, 'rollout', 'status', 'deployment/frontend',
                '--timeout=' + str(config['timeout']) + 's', timeout=config['timeout'] + 45)
        current = [p for p in get('pods')['items'] if p['metadata'].get('labels', {}).get('app') == 'frontend']
        if len(current) != replicas or not all(ready(p) and p['spec']['nodeName'] != node for p in current):
            raise RuntimeError('Frontend has not recovered off the drained node')
        result['frontend_recovered_off_target'] = True
        result['status'] = 'PASS'
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        stop.set()
        if monitor_thread:
            monitor_thread.join(timeout=8)
        errors = []
        if touched:
            # Independent cleanup attempts: a PDB restore failure must not skip uncordon.
            for label, action in [('restore_pdb', restore_pdb),
                                  ('uncordon', lambda: kubectl('uncordon', node))]:
                try:
                    action()
                except Exception as exc:
                    errors.append(label + ': ' + str(exc))
            try:
                snapshot('after-cleanup')
            except Exception as exc:
                errors.append('snapshot: ' + str(exc))
        result['cleanup_errors'] = errors
        if errors:
            result['status'] = 'FAIL'
        save('result.json', result)
    return result


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise RuntimeError('Interrupted by signal ' + str(signum))
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    result = run(json.load(sys.stdin))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status'] == 'PASS' else 1)
