"""Local HTTPS identity, certificate distribution and the owned Caddy process."""
from __future__ import annotations

import hashlib
import html
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
from string import Template
import urllib.request
from urllib.parse import urlsplit


class LanHttps:
    def __init__(self, data_root, adapter, *, https_port=None, certificate_port=None, domain=None, import_from=None):
        self.root = Path(data_root) / "runtime" / "lan-https"
        self.adapter = adapter
        self.import_from = Path(import_from).resolve() if import_from else None
        saved_path = self.root / "connection.json"
        if saved_path.exists():
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
        elif self.import_from:
            legacy = json.loads((self.import_from / "config.json").read_text(encoding="utf-8-sig"))
            saved = {"address": legacy["Address"], "domain": legacy["Domain"],
                "https_port": legacy["HttpsPort"], "certificate_port": legacy["CertificatePort"]}
        else:
            saved = {}
        if saved.get("address", adapter.ipv4) != adapter.ipv4:
            source = saved_path if saved_path.exists() else self.import_from / "config.json"
            raise RuntimeError(f"{adapter.name} 当前 IPv4 {adapter.ipv4} 与保存的 {saved['address']} 不一致：{source}。请修正网络或该配置；不会自动更换域名和 CA。")
        self.ca_fingerprint = saved.get("ca_sha256")
        self.domain = (domain or saved.get("domain") or f"linkarium.{adapter.ipv4.replace('.', '-')}.sslip.io").lower()
        if (len(self.domain) > 253 or "." not in self.domain or
                any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in self.domain.split("."))):
            raise RuntimeError("局域网 HTTPS 需要有效的域名，不能使用原始 IP、路径或端口。")
        import ipaddress
        try:
            ipaddress.ip_address(self.domain)
        except ValueError:
            pass
        else:
            raise RuntimeError("Passkey RP ID 需要域名，不能使用原始 IP。")
        self.https_port = int(https_port if https_port is not None else saved.get("https_port", 8443))
        self.certificate_port = int(certificate_port if certificate_port is not None else saved.get("certificate_port", 8081))
        if not all(1 <= port <= 65535 for port in (self.https_port, self.certificate_port)) or self.https_port == self.certificate_port:
            raise RuntimeError("HTTPS 和证书安装页需要两个不同的有效端口。")
        self.origin = f"https://{self.domain}" + (f":{self.https_port}" if self.https_port != 443 else "")
        self.certificate_url = f"http://{adapter.ipv4}:{self.certificate_port}/"
        self.public = self.root / "public"
        self.ca_path = self.public / "linkarium-ca.crt"
        self.config_path = self.root / "Caddyfile"
        candidate = os.environ.get("BOOKMARK_CADDY") or shutil.which("caddy")
        if not candidate:
            project = Path(__file__).resolve().parents[2]
            candidate = project / "tools" / "caddy" / ("caddy.exe" if os.name == "nt" else "caddy")
        self.caddy = Path(candidate).resolve()
        if not self.caddy.is_file():
            raise RuntimeError("缺少 Caddy；请先运行 scripts/install-https.ps1，或用 BOOKMARK_CADDY 指定已安装的程序。")

    def configure(self, backend_port):
        connection = {"mode": "lan_https", "adapter": self.adapter.name, "ipv4": self.adapter.ipv4,
            "access_url": self.origin + "/", "certificate_url": self.certificate_url}
        os.environ.update(BOOKMARK_CONNECTION=json.dumps(connection), BOOKMARK_WEB_ADDRESSES='["127.0.0.1"]',
            BOOKMARK_PASSKEY_ORIGIN=self.origin, BOOKMARK_HTTPS="1", BOOKMARK_TRUST_PROXY="1",
            BOOKMARK_PROXY_PEER="127.0.0.1", BOOKMARK_CSRF_ORIGINS=self.origin,
            BOOKMARK_ALLOWED_HOSTS=f"{self.domain},localhost,127.0.0.1,[::1]")
        self.backend_port = backend_port

    def prepare(self):
        for port in (self.https_port, self.certificate_port):
            with socket.socket() as probe:
                if os.name == "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    probe.bind((self.adapter.ipv4, port))
                except OSError:
                    raise RuntimeError(f"局域网端口已被占用或无法绑定：{self.adapter.ipv4}:{port}") from None
        self.public.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.import_from:
            self._import_ca()
        authority = self.root / "caddy" / "pki" / "authorities" / "local"
        if (self.root / "connection.json").exists() and not all((authority / name).is_file() for name in ("root.crt", "root.key")):
            raise RuntimeError("持久 CA 资料缺失；请恢复原 CA，不会生成替代身份。")
        if self.ca_fingerprint and self._fingerprint(authority / "root.crt") != self.ca_fingerprint:
            raise RuntimeError("CA 指纹与保存的 HTTPS 身份不一致，未启动替代 CA。")
        storage = (self.root / "caddy").as_posix()
        public = self.public.as_posix()
        template = Template("""{
    admin off
    persist_config off
    auto_https disable_redirects
    skip_install_trust
    storage file_system $STORAGE
    servers {
        protocols h1 h2
    }
    pki {
        ca local {
            name "Linkarium Wi-Fi CA"
        }
    }
}
https://$DOMAIN:$TLS_PORT {
    bind $ADDRESS
    tls internal
    reverse_proxy 127.0.0.1:$BACKEND_PORT {
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto https
        header_up -Forwarded
        header_up -X-Forwarded-Host
        header_up -X-Forwarded-Port
    }
}
http://$ADDRESS:$CERT_PORT {
    bind $ADDRESS
    @certificate path /linkarium-ca.crt
    handle @certificate {
        root * $PUBLIC
        header Content-Disposition "attachment; filename=linkarium-ca.crt"
        file_server
    }
    @instructions path /
    handle @instructions {
        root * $PUBLIC
        file_server
    }
    handle {
        respond 404
    }
}
""")
        self.config_path.write_text(template.substitute(STORAGE=json.dumps(storage, ensure_ascii=False),
            PUBLIC=json.dumps(public, ensure_ascii=False), DOMAIN=self.domain, TLS_PORT=self.https_port,
            CERT_PORT=self.certificate_port, BACKEND_PORT=self.backend_port, ADDRESS=self.adapter.ipv4), encoding="utf-8")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        subprocess.run([str(self.caddy), "validate", "--config", str(self.config_path), "--adapter", "caddyfile"],
            check=True, timeout=30, env=self.process_environment(), **options)
        root_ca = self.root / "caddy" / "pki" / "authorities" / "local" / "root.crt"
        shutil.copyfile(root_ca, self.ca_path)
        ca_fingerprint = self._fingerprint(self.ca_path)
        fingerprint = ca_fingerprint.upper()
        fingerprint = ":".join(fingerprint[index:index+2] for index in range(0, len(fingerprint), 2))
        page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Linkarium Android 连接</title>
<style>body{font:17px/1.7 system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 24px;color:#182b48}li{margin:18px 0}code{overflow-wrap:anywhere;font-size:13px}a{color:#175de5}</style>
<h1>Linkarium Android 连接</h1><ol>
<li><a href="/linkarium-ca.crt" download>下载 CA 证书</a></li>
<li>在 Android 设置中搜索“安装证书”，选择“CA 证书”，安装下载的 linkarium-ca.crt。</li>
<li><a href="ORIGIN/login">打开 Linkarium HTTPS</a></li>
<li>使用电脑提供的添加链接和独立授权码创建 Passkey；首次使用该域名时，使用服务器终端提供的首次授权码。</li></ol>
<p>CA 证书 SHA-256：<br><code>FINGERPRINT</code></p></html>
""".replace("ORIGIN", html.escape(self.origin, quote=True)).replace("FINGERPRINT", fingerprint)
        (self.public / "index.html").write_text(page, encoding="utf-8")
        saved = {"version": 1, "address": self.adapter.ipv4, "domain": self.domain,
            "https_port": self.https_port, "certificate_port": self.certificate_port, "ca_sha256": ca_fingerprint}
        pending = self.root / "connection.json.tmp"
        pending.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pending.replace(self.root / "connection.json")

    @staticmethod
    def _fingerprint(path):
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(path.read_text(encoding="ascii"))).hexdigest()

    def _import_ca(self):
        source = self.import_from / "state" / "caddy" / "pki" / "authorities" / "local"
        destination = self.root / "caddy" / "pki" / "authorities" / "local"
        names = ("root.crt", "root.key", "intermediate.crt", "intermediate.key")
        if not all((source / name).is_file() for name in names):
            raise RuntimeError("原 HTTPS 配置缺少完整的 CA 资料，未生成替代 CA。")
        if destination.exists() and any(destination.iterdir()):
            if not all((destination / name).is_file() and
                    (destination / name).read_bytes() == (source / name).read_bytes() for name in ("root.crt", "root.key")):
                raise RuntimeError("目标 CA 与导入 CA 不一致，未覆盖已有证书。")
            return
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in names:
            shutil.copyfile(source / name, destination / name)
            (destination / name).chmod(0o600)

    def process_environment(self):
        # Keep Caddy caches and config entirely inside the dedicated runtime root.
        return {**os.environ, "XDG_DATA_HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root)}

    def command(self):
        return [str(self.caddy), "run", "--config", str(self.config_path), "--adapter", "caddyfile"]

    def ready(self):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.certificate_url + "linkarium-ca.crt", timeout=2) as certificate:
                if certificate.status != 200 or certificate.read(65536) != self.ca_path.read_bytes():
                    return False
            context = ssl.create_default_context(cafile=self.ca_path)
            # Probe the advertised name through the host resolver and network path.
            # Direct IP + SNI alone could announce an unusable canonical URL.
            with socket.create_connection((self.domain, self.https_port), timeout=2) as raw:
                with context.wrap_socket(raw, server_hostname=self.domain) as channel:
                    channel.sendall(f"GET /health/ready HTTP/1.1\r\nHost: {urlsplit(self.origin).netloc}\r\nConnection: close\r\n\r\n".encode("ascii"))
                    response = http.client.HTTPResponse(channel)
                    response.begin()
                    return response.status == 200 and json.loads(response.read(4096)).get("ready") is True
        except (OSError, ValueError, http.client.HTTPException):
            return False
