"""Print a bcrypt hash for config.yaml.

docker run --rm -it -v "$PWD":/app db-viewer-dev python hashpw.py
"""
import getpass

from auth import hash_password

MIN_LENGTH = 10


def main() -> None:
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Repeat: "):
        raise SystemExit("Passwords don't match.")
    if len(password) < MIN_LENGTH:
        raise SystemExit(f"Use at least {MIN_LENGTH} characters.")
    print(hash_password(password))


if __name__ == "__main__":
    main()
