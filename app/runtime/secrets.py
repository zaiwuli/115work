import os
from pathlib import Path

from cryptography.fernet import Fernet


class SecretStore:
    def __init__(self, key_path: Path):
        if not key_path.exists():
            # Exclusive creation prevents competing processes from replacing a key.
            fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
        self.fernet = Fernet(key_path.read_bytes())

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.fernet.decrypt(value.encode()).decode()
