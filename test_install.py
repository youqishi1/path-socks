"""Disposable Linux CI VM only: exercise installer with local ACME/download fixtures.

This does NOT test real Cloudflare permissions, public DNS or Let's Encrypt.
"""
import os
from pathlib import Path
import pty
import select
import socket
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def interactive_install():
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("bash", ["bash", str(ROOT / "install-path.sh"), "gateway.test"], os.environ)
    os.write(fd, b"\nfixture-token-not-a-real-secret\n")
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 1)
        if readable:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            print(data.decode(errors="replace"), end="", flush=True)
    else:
        os.kill(pid, 9)
        raise AssertionError("installer timed out")
    os.close(fd)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, status


def main():
    assert os.geteuid() == 0 and os.environ.get("GITHUB_ACTIONS") == "true", "Disposable GitHub runner only"
    assert not Path("/etc/path-socks").exists(), "Refuse to modify an existing installation"
    listeners = []
    for number in (80, 443, 18080, 25443):
        listener = socket.socket()
        try:
            listener.bind(("0.0.0.0", number))
            listener.listen()
            listeners.append(listener)
        except OSError:
            listener.close()  # An existing runner service is also a valid occupant.

    with tempfile.TemporaryDirectory(prefix="path-socks-ci-") as directory:
        fixtures = Path(directory)
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=gateway.test",
            "-addext", "subjectAltName=DNS:gateway.test", "-keyout", str(fixtures / "key.pem"), "-out", str(fixtures / "cert.pem"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        mock = '''#!/usr/bin/env python3
import json, os, shutil, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
name = Path(sys.argv[0]).name
root = Path(os.environ["FIXTURE_ROOT"])
if name == "apt-get":
    sys.exit(0)
if name == "curl":
    url = next((x for x in args if x.startswith("https://")), "")
    if url.startswith("https://api.github.com/"):
        print(json.dumps({"sha": "a" * 40}))
    elif url.startswith("https://raw.githubusercontent.com/"):
        relative = url.split("/", 6)[6]
        shutil.copyfile(root / relative, args[args.index("-o") + 1])
    else:
        sys.exit(subprocess.call(["/usr/bin/curl", *args]))
elif name == "certbot":
    if args == ["plugins"]:
        print("dns-cloudflare")
    elif args[0] == "certonly":
        base = Path(args[args.index("--config-dir") + 1])
        cert = base / "live" / args[args.index("--cert-name") + 1]
        cert.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(os.environ["FIXTURES"]) / "cert.pem", cert / "fullchain.pem")
        shutil.copyfile(Path(os.environ["FIXTURES"]) / "key.pem", cert / "privkey.pem")
    elif args[0] != "renew":
        sys.exit(1)
'''
        for name in ("curl", "apt-get", "certbot"):
            target = fixtures / name
            target.write_text(mock)
            target.chmod(0o755)
        os.environ.update(FIXTURE_ROOT=str(ROOT), FIXTURES=str(fixtures), CURL_CA_BUNDLE=str(fixtures / "cert.pem"))
        os.environ["PATH"] = str(fixtures) + ":" + os.environ["PATH"]
        interactive_install()
        state = Path("/etc/path-socks")
        first_port = int((state / "port").read_text())
        assert 20000 <= first_port < 30000 and first_port != 25443
        first_users = (state / "users.db").read_bytes()
        assert (state / "cloudflare.ini").stat().st_mode & 0o777 == 0o600
        run("systemctl", "is-active", "path-socks")
        run("systemctl", "is-active", "path-socks-renew.timer")
        interactive_install()
        assert int((state / "port").read_text()) == first_port, "upgrade changed an owned port"
        assert (state / "users.db").read_bytes() == first_users, "upgrade changed UUIDs"
        run("bash", "/opt/path-socks/renew.sh")
        run("bash", "/usr/local/bin/sbb", "status")
        for listener in listeners:
            with socket.create_connection(("127.0.0.1", listener.getsockname()[1]), timeout=2):
                pass
        run("systemctl", "stop", "path-socks", "path-socks-renew.timer")
        print("PASS: occupied common ports preserved; TLS install, re-install and renewal fixture passed")
    for listener in listeners:
        listener.close()


if __name__ == "__main__":
    main()
