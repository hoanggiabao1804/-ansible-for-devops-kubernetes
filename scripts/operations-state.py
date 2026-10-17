#!/usr/bin/env python3
"""Read deployment identity for idempotency comparisons; no Secret/token export."""
import argparse
import json
import subprocess


def capture(kubeconfig, helm='/usr/local/bin/helm'):
    def get(kind):
        command = ['kubectl', '--kubeconfig', kubeconfig, '--request-timeout=30s',
                   'get', kind, '-o', 'json']
        if kind != 'nodes':
            command += ['-A']
        return json.loads(subprocess.check_output(command, timeout=45))['items']

    def key(obj):
        meta = obj['metadata']
        return meta.get('namespace', '') + '/' + meta['name']

    releases = json.loads(subprocess.check_output([
        helm, 'list', '-A', '--all', '--kubeconfig', kubeconfig, '-o', 'json'], timeout=45))
    return {
        'helm': {x['namespace'] + '/' + x['name']: {
            k: x[k] for k in ('revision', 'status', 'chart', 'app_version')}
            for x in releases},
        'nodes': {key(x): {'uid': x['metadata']['uid'],
                          'unschedulable': x.get('spec', {}).get('unschedulable', False),
                          'ready': next((c['status'] for c in x['status'].get('conditions', [])
                                         if c['type'] == 'Ready'), None)} for x in get('nodes')},
        'pods': {key(x): {'uid': x['metadata']['uid'], 'node': x['spec'].get('nodeName'),
                         'phase': x['status'].get('phase'),
                         'containers': {c['name']: {'id': c.get('containerID'),
                                                   'restarts': c.get('restartCount'),
                                                   'ready': c.get('ready')}
                                        for c in x['status'].get('containerStatuses', [])}}
                 for x in get('pods') if x['status'].get('phase') not in ('Succeeded', 'Failed')},
        'deployments': {key(x): {'generation': x['metadata']['generation'], 'spec': x['spec'],
                                 'ready': x['status'].get('readyReplicas', 0),
                                 'available': x['status'].get('availableReplicas', 0)}
                        for x in get('deployments')},
        'ingresses': {key(x): x['spec'] for x in get('ingresses')},
        'pdbs': {key(x): x['spec'] for x in get('poddisruptionbudgets')},
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--helm', default='/usr/local/bin/helm')
    args = parser.parse_args()
    print(json.dumps(capture(args.kubeconfig, args.helm), indent=2, sort_keys=True))
