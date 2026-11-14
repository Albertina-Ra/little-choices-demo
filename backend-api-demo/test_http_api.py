"""Exercise the real HTTP boundary, including races and rejected requests."""
import concurrent.futures
import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from server import DemoServer, MAX_BODY, MAX_ROWS


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = DemoServer(('127.0.0.1', 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.sample = (Path(__file__).parent / 'sample.csv').read_text(encoding='utf-8-sig')
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def request(self, path, body=None, key=None, headers=None, raw=None):
        headers = dict(headers or {})
        if body is not None or raw is not None:
            headers.setdefault('Content-Type', 'application/json')
            headers.setdefault('Idempotency-Key', key or uuid.uuid4().hex)
            data = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode('utf-8')
        else:
            data = None
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_sample_and_health(self):
        status, sample = self.request('/api/sample')
        self.assertEqual(status, 200)
        self.assertTrue(sample['synthetic'])
        self.assertEqual(sample['csv'], self.sample)
        self.assertEqual(self.request('/api/health')[1]['mode'], 'local_demo')

    def test_valid_result_and_identifier_strings(self):
        status, result = self.request('/api/cleanup', {'csv': self.sample})
        self.assertEqual(status, 200)
        self.assertEqual(result['summary'], {'input': 26, 'retained': 21, 'duplicates': 1, 'quarantined': 4})
        self.assertEqual(result['report']['clean'][0]['订单号'], '00000001')
        self.assertTrue(any('字段冲突' in issue['reason'] for issue in result['report']['issues']))
        self.assertTrue(any('日期不存在' in issue['reason'] for issue in result['report']['issues']))
        self.assertTrue(any('金额' in issue['reason'] for issue in result['report']['issues']))
        self.assertFalse(result['replayed'])

    def test_replay_does_not_process_twice(self):
        key = uuid.uuid4().hex
        before = self.request('/api/health')[1]['processed_requests']
        _, first = self.request('/api/cleanup', {'csv': self.sample}, key)
        status, second = self.request('/api/cleanup', {'csv': self.sample}, key)
        after = self.request('/api/health')[1]['processed_requests']
        self.assertEqual(status, 200)
        self.assertEqual(first['request_id'], second['request_id'])
        self.assertTrue(second['replayed'])
        self.assertEqual(after - before, 1)

    def test_conflicting_content_same_key(self):
        key = uuid.uuid4().hex
        self.request('/api/cleanup', {'csv': self.sample}, key)
        status, error = self.request('/api/cleanup', {'csv': self.sample.replace('13.79', '13.78')}, key)
        self.assertEqual(status, 409)
        self.assertIn('不同内容', error['error'])

    def test_concurrent_same_key_processes_once(self):
        key = uuid.uuid4().hex
        before = self.request('/api/health')[1]['processed_requests']
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.request('/api/cleanup', {'csv': self.sample}, key), range(12)))
        self.assertEqual({status for status, _ in results}, {200})
        self.assertEqual(len({r['request_id'] for _, r in results}), 1)
        self.assertEqual(sum(not r['replayed'] for _, r in results), 1)
        self.assertEqual(self.request('/api/health')[1]['processed_requests'] - before, 1)

    def test_json_and_schema_rejections(self):
        for body in ([], {'wrong': 'field'}, {'csv': []}, {'csv': ''}, {'csv': 'A,B\n1,2\n'}, {'csv': '店铺,店铺\n甲,乙\n'}):
            with self.subTest(body=body):
                self.assertEqual(self.request('/api/cleanup', body)[0], 400)
        self.assertEqual(self.request('/api/cleanup', raw=b'{broken')[0], 400)
        self.assertEqual(self.request('/api/cleanup', raw=b'\xff')[0], 400)

    def test_format_key_origin_and_path_rejections(self):
        body = {'csv': self.sample}
        self.assertEqual(self.request('/api/cleanup', body, headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('/api/cleanup', body, headers={'Idempotency-Key': 'bad key'})[0], 400)
        self.assertEqual(self.request('/api/cleanup', body, headers={'Origin': 'https://example.invalid'})[0], 403)
        self.assertEqual(self.request('/api/health', headers={'Host': 'unexpected.invalid'})[0], 403)
        self.assertEqual(self.request('/missing')[0], 404)

    def test_body_and_row_limits(self):
        self.assertEqual(self.request('/api/cleanup', raw=b'x' * (MAX_BODY + 1))[0], 413)
        lines = self.sample.splitlines()
        excessive = lines[0] + '\n' + (lines[1] + '\n') * (MAX_ROWS + 1)
        self.assertEqual(self.request('/api/cleanup', {'csv': excessive})[0], 400)
        broken = lines[0] + '\nonly,three,columns\n'
        self.assertEqual(self.request('/api/cleanup', {'csv': broken})[0], 400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
