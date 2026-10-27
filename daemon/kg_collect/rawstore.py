"""RAW layer: content-addressed files OUTSIDE git (data_store/raw/<host>/<aa>/<sha256>.<ext>). Rows only keep the reference + hash."""
import hashlib
import os


class RawStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    @staticmethod
    def sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def put(self, host: str, data: bytes, ext: str) -> tuple[str, str]:
        h = self.sha(data)
        rel = os.path.join(host or '_', h[:2], f'{h}.{ext}')
        path = os.path.join(self.root, rel)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(data)
        return rel, h

    def read(self, rel: str) -> bytes:
        with open(os.path.join(self.root, rel), 'rb') as f:
            return f.read()

    def put_text(self, host: str, text: str) -> str:
        return self.put(host, text.encode('utf-8'), 'txt')[0]

    def read_text(self, rel: str) -> str:
        return self.read(rel).decode('utf-8')
