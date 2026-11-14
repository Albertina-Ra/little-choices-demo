"""Loopback-only, dependency-free API portfolio demo. Not a production service."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import threading
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from order_engine import FIELDS, analyze

ROOT = Path(__file__).resolve().parent
MAX_BODY = 256 * 1024
MAX_ROWS = 1000
MAX_KEYS = 128


class InputError(ValueError):
    pass


def parse_csv(content):
    if not isinstance(content, str) or not content.strip():
        raise InputError('csv 必须是非空文本')
    if '\x00' in content:
        raise InputError('CSV 含有无效的空字符')
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff'), newline=''))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or set(headers) != set(FIELDS):
        raise InputError('列名必须恰好包含：' + '、'.join(FIELDS))
    records = []
    for row in reader:
        if len(records) >= MAX_ROWS:
            raise InputError(f'演示接口每次最多处理 {MAX_ROWS} 行')
        if None in row or any(value is None for value in row.values()):
            raise InputError(f'第 {reader.line_num} 物理行的列数不正确')
        records.append({'source': 'request.csv', 'line': reader.line_num, 'raw': row})
    if not records:
        raise InputError('CSV 至少需要一条数据')
    return records


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, Handler)
        self.results = OrderedDict()
        self.result_lock = threading.Lock()
        self.processed_requests = 0


class Handler(BaseHTTPRequestHandler):
    server_version = 'OrderDemo/1.0'

    def log_message(self, *_args):
        pass

    def send(self, status, payload, content_type='application/json; charset=utf-8'):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        elif isinstance(payload, str):
            payload = payload.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def allowed_request(self):
        hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if self.headers.get('Host') not in hosts:
            self.send(403, {'error': '只接受本机地址'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://{host}' for host in hosts}:
            self.send(403, {'error': '只接受演示页面的同源请求'})
            return False
        return True

    def do_GET(self):
        if not self.allowed_request():
            return
        path = urlsplit(self.path).path
        if path == '/':
            self.send(200, (ROOT / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        elif path == '/api/health':
            self.send(200, {'ok': True, 'mode': 'local_demo', 'max_rows': MAX_ROWS, 'processed_requests': self.server.processed_requests})
        elif path == '/api/sample':
            self.send(200, {'synthetic': True, 'csv': (ROOT / 'sample.csv').read_text(encoding='utf-8-sig')})
        elif path == '/favicon.ico':
            self.send(204, b'')
        else:
            self.send(404, {'error': '接口不存在'})

    def do_POST(self):
        if not self.allowed_request():
            return
        if urlsplit(self.path).path != '/api/cleanup':
            self.send(404, {'error': '接口不存在'})
            return
        key = self.headers.get('Idempotency-Key', '')
        if not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}', key):
            self.send(400, {'error': '需要合法的 Idempotency-Key 请求头，长度1至64'})
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            self.send(415, {'error': '请求格式须为 application/json'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.send(400, {'error': 'Content-Length 无效'})
            return
        if not 0 < size <= MAX_BODY:
            self.send(413, {'error': '请求内容须为1至262144字节'})
            return
        try:
            body = json.loads(self.rfile.read(size).decode('utf-8'))
            if not isinstance(body, dict) or set(body) != {'csv'}:
                raise InputError('请求体须为只含 csv 字段的JSON对象')
            records = parse_csv(body['csv'])
        except (UnicodeError, json.JSONDecodeError, csv.Error, InputError, RecursionError) as exc:
            self.send(400, {'error': str(exc)})
            return
        fingerprint = hashlib.sha256(body['csv'].encode('utf-8')).hexdigest()
        with self.server.result_lock:
            cached = self.server.results.get(key)
            if cached:
                old_fingerprint, result = cached
                if old_fingerprint != fingerprint:
                    self.send(409, {'error': '同一请求号不能用于不同内容，请创建新请求'})
                    return
                self.send(200, {**result, 'replayed': True})
                return
            report = analyze(records)
            report.pop('synthetic', None)
            result = {
                'request_id': uuid.uuid4().hex,
                'demo_environment': True,
                'replayed': False,
                'summary': {'input': report['input_rows'], 'retained': len(report['clean']), 'duplicates': len(report['duplicates']), 'quarantined': len(report['issues'])},
                'report': report,
            }
            self.server.processed_requests += 1
            self.server.results[key] = (fingerprint, result)
            if len(self.server.results) > MAX_KEYS:
                self.server.results.popitem(last=False)
            self.send(200, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8876)
    args = parser.parse_args()
    with DemoServer(('127.0.0.1', args.port)) as server:
        print(f'ORDER_API_DEMO http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
