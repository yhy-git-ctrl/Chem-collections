"""小规模连通测试：只发送合成文字/纯色图，不读取或修改文献。"""
from concurrent.futures import ThreadPoolExecutor
import base64
import json
from pathlib import Path
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import llm


def synthetic_png():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 32, 32, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\0\0' * 32) * 32)) + chunk(b'IEND', b''))


def check(role):
    try:
        if role == 'text':
            messages = [{'role': 'user', 'content': 'Connection test. Reply with OK only.'}]
        else:
            url = 'data:image/png;base64,' + base64.b64encode(synthetic_png()).decode()
            messages = [{'role': 'user', 'content': [
                {'type': 'text', 'text': 'What is the main color? Reply with one English color word.'},
                {'type': 'image_url', 'image_url': {'url': url}},
            ]}]
        content = llm.complete(role, messages)
        expected = 'ok' if role == 'text' else 'red'
        print(json.dumps({'role': role, 'success': expected in content.lower(), 'response_chars': len(content)}))
        return expected in content.lower()
    except Exception as exc:
        print(json.dumps({'role': role, 'success': False, 'error_type': type(exc).__name__, 'http_status': getattr(exc, 'status_code', None)}))
        return False


if __name__ == '__main__':
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(check, ['text', 'vision']))
    sys.exit(0 if all(results) else 1)
