"""在成品容器内检查版本、原生依赖及 HTTP/SMTP 服务启动。"""

import json
import os
import secrets
import smtplib
import ssl
import subprocess
import sys
import time
from importlib import import_module
from urllib.error import URLError
from urllib.request import urlopen

from cryptography.fernet import Fernet


def main() -> None:
    """使用临时凭据和容器本地数据库启动真实命令入口。"""
    import_module("asyncpg")
    assert ssl.create_default_context().cert_store_stats()["x509_ca"] > 0
    result = subprocess.run(
        ["/usr/local/bin/smtp-gateway", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == sys.argv[1], result.stdout

    env = {
        **os.environ,
        "SMTP_GATEWAY_HTTP__AUTH__USERNAME": "image-check",
        "SMTP_GATEWAY_HTTP__AUTH__PASSWORD": secrets.token_urlsafe(32),
        "SMTP_GATEWAY_SMTP__AUTH__PASSWORD": secrets.token_urlsafe(32),
        "SMTP_GATEWAY_SMTP__ALLOW_INSECURE_AUTH": "true",
        "SMTP_GATEWAY_DATABASE__ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }
    with subprocess.Popen(["/usr/local/bin/smtp-gateway"], env=env) as server:
        try:
            for _ in range(60):
                assert server.poll() is None, "Server exited before becoming healthy"
                try:
                    with urlopen(
                        "http://127.0.0.1:18081/healthz", timeout=1
                    ) as response:
                        assert json.load(response) == {"status": "ok"}
                    break
                except URLError, TimeoutError:
                    time.sleep(0.5)
            else:
                raise TimeoutError("Server did not become healthy")

            with smtplib.SMTP("127.0.0.1", 1025, timeout=5) as smtp:
                assert smtp.ehlo()[0] == 250
                assert "LOGIN" in smtp.esmtp_features["auth"]
                smtp.login("grafana", env["SMTP_GATEWAY_SMTP__AUTH__PASSWORD"])
                assert smtp.noop()[0] == 250
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    main()
