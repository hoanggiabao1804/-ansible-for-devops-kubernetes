"""Offline contracts for integration; never contact Kubernetes or create VMs.

Run: python3 -m unittest discover -s tests -v
"""
from email.message import Message
from http.cookiejar import CookieJar
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


smoke = module('checkout-smoke')
drain = module('drain-experiment')
idem = module('check-idempotency')


class NodeAffinity(unittest.TestCase):
    def test_inventory_nodes_use_single_value_or_terms(self):
        import yaml
        from jinja2.nativetypes import NativeEnvironment
        affinity = yaml.safe_load((ROOT / 'vars/operations.yml').read_text())['operations_frontend_affinity']
        expression = affinity['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']
        for workers in (['node1'], ['node1', 'node2'], ['node1', 'node2', 'node3']):
            terms = NativeEnvironment().from_string(expression).render(groups={'k8s_nodes': workers})
            self.assertIsInstance(terms, list)
            self.assertEqual(len(terms), len(workers))
            selected = []
            for term in terms:
                requirement, = term['matchFields']
                self.assertEqual(requirement['key'], 'metadata.name')
                self.assertEqual(requirement['operator'], 'In')
                self.assertEqual(len(requirement['values']), 1)
                selected.extend(requirement['values'])
            self.assertEqual(selected, workers)


class HTTPIntegration(unittest.TestCase):
    def test_absolute_ingress_redirect_keeps_private_endpoint_and_host(self):
        handler = smoke.RecordedRedirects('http://192.168.56.11:30080', 'boutique.local')
        request = Request('http://192.168.56.11:30080/cart', data=b'x=1', headers={'Host': 'boutique.local'})
        redirected = handler.redirect_request(request, None, 302, 'Found', {}, 'http://boutique.local/cart')
        self.assertEqual(redirected.full_url, 'http://192.168.56.11:30080/cart')
        self.assertEqual(redirected.get_header('Host'), 'boutique.local')
        self.assertEqual(redirected.get_method(), 'GET')

    def test_redirect_virtual_host_with_nodeport_is_accepted(self):
        handler = smoke.RecordedRedirects('http://192.168.56.11:30080', 'boutique.local')
        redirected = handler.redirect_request(Request('http://192.168.56.11:30080/cart'), None,
                                              302, 'Found', {}, 'http://boutique.local:30080/cart')
        self.assertEqual(redirected.full_url, 'http://192.168.56.11:30080/cart')

    def test_redirect_cannot_escape_lab_origin(self):
        handler = smoke.RecordedRedirects('http://192.168.56.11:30080', 'boutique.local')
        with self.assertRaises(RuntimeError):
            handler.redirect_request(Request('http://192.168.56.11:30080/'), None, 302,
                                     'Found', {}, 'http://example.com/cart')

    def test_host_header_preserves_session_cookie_on_private_endpoint(self):
        headers = Message()
        headers['Set-Cookie'] = 'shop_session-id=demo-session; Path=/'
        response = type('Response', (), {'info': lambda self: headers})()
        jar = CookieJar()
        first = Request('http://192.168.56.11:30080/', headers={'Host': 'boutique.local'})
        jar.extract_cookies(response, first)
        second = Request('http://192.168.56.11:30080/cart', headers={'Host': 'boutique.local'})
        jar.add_cookie_header(second)
        self.assertEqual(second.get_header('Cookie'), 'shop_session-id=demo-session')

    def test_header_injection_rejected(self):
        with self.assertRaises(Exception):
            smoke.validate_host_header('boutique.local\r\nX-Test: true')


class Recap(unittest.TestCase):
    def test_pass_needs_recap_and_zero_changes(self):
        text = 'PLAY RECAP ***\nmaster : ok=8 changed=0 unreachable=0 failed=0 skipped=1 rescued=0 ignored=0\n'
        self.assertIn('master', idem.assert_recap(text))
        for broken in (text.replace('changed=0', 'changed=1'), text.replace('rescued=0', 'rescued=1'), 'no recap'):
            with self.assertRaises(RuntimeError):
                idem.assert_recap(broken)


class DrainLifecycle(unittest.TestCase):
    def simulate(self, fail_allowed=False, initially_cordoned=False):
        calls = []
        original_spec = {'minAvailable': 1, 'selector': {'matchLabels': {'app': 'frontend'}}}
        state = {'spec': original_spec.copy(), 'cordoned': initially_cordoned, 'moved': False}
        def pod(name, node):
            return {'metadata': {'name': name, 'namespace': 'microservice', 'uid': name,
                                 'labels': {'app': 'frontend'}}, 'spec': {'nodeName': node},
                    'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        def run(command, **kwargs):
            args = command[4:]
            calls.append(args)
            if args[:2] == ['-n', 'microservice']:
                args = args[2:]
            rc, err, body = 0, '', ''
            if args[0] == 'get':
                kind = args[1]
                items = []
                if kind == 'nodes':
                    items = [{'metadata': {'name': name}, 'spec': {'unschedulable': state['cordoned'] if name == 'node1' else False},
                              'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                             for name in ('node1', 'node2')]
                elif kind == 'pods':
                    items = [pod('new' if state['moved'] else 'first', 'node2' if state['moved'] else 'node1'), pod('second', 'node2')]
                if kind == 'deployment':
                    obj = {'spec': {'replicas': 2}}
                elif kind == 'pdb' and args[2] == 'frontend':
                    obj = {'metadata': {'generation': 2}, 'spec': state['spec'],
                           'status': {'observedGeneration': 2, 'disruptionsAllowed': 2-state['spec']['minAvailable']}}
                else:
                    obj = {'items': items}
                body = json.dumps(obj)
            elif args[0] == 'patch':
                state['spec'] = json.loads(args[args.index('-p')+1])[0]['value']
            elif args[0] == 'drain':
                state['cordoned'] = True
                if state['spec']['minAvailable'] == 2:
                    rc, err = 1, 'Cannot evict pod as it would violate the pod disruption budget.'
                elif fail_allowed:
                    rc, err = 1, 'Insufficient capacity; drain timed out'
                else:
                    state['moved'] = True
            elif args[0] == 'uncordon':
                state['cordoned'] = False
            return subprocess.CompletedProcess(command, rc, body, err)
        with tempfile.TemporaryDirectory() as directory:
            config = {'output': directory+'/run', 'namespace': 'microservice', 'node': 'node1',
                      'workers': ['node1','node2'], 'scope': 'all', 'delete_emptydir': False,
                      'timeout': 30, 'kubeconfig': '/unused', 'endpoint': 'http://127.0.0.1', 'host': 'boutique.local'}
            with patch.object(drain.subprocess, 'run', side_effect=run), patch.object(drain.threading, 'Thread'):
                result = drain.run(config)
        return result, state, calls, original_spec

    def test_blocked_then_allowed_and_restored(self):
        result, state, calls, original = self.simulate()
        self.assertEqual(result['status'], 'PASS', result)
        self.assertFalse(state['cordoned'])
        self.assertEqual(state['spec'], original)
        drains = [c for c in calls if c[0] == 'drain']
        self.assertEqual(len(drains), 2)
        self.assertIn('--pod-selector=app=frontend', drains[0])
        self.assertNotIn('--pod-selector=app=frontend', drains[1])
        self.assertFalse(any('--force' in c or '--disable-eviction' in c for c in calls))

    def test_allowed_drain_failure_still_restores(self):
        result, state, _, original = self.simulate(fail_allowed=True)
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(state['cordoned'])
        self.assertEqual(state['spec'], original)

    def test_preexisting_cordon_is_not_undone(self):
        result, state, calls, _ = self.simulate(initially_cordoned=True)
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(state['cordoned'])
        self.assertFalse(any(c[0] in ('drain', 'uncordon', 'patch') for c in calls))


if __name__ == '__main__':
    unittest.main()
