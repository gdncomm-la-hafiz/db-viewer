"""Password hashing for config.yaml users."""
import bcrypt

MAX_PASSWORD_BYTES = 72  # bcrypt ignores (or rejects) anything longer
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt()).decode()


def hash_password(password: str) -> str:
    raw = password.encode()
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password is longer than {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode()


def check_login(users: dict[str, str], username: str, password: str) -> bool:
    # Check against a dummy hash for unknown users so timing doesn't reveal who exists.
    hashed = users.get(username, _DUMMY_HASH)
    raw = password.encode()
    if len(raw) > MAX_PASSWORD_BYTES:
        return False
    return bcrypt.checkpw(raw, hashed.encode()) and username in users
