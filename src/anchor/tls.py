"""TLS primitives shared with the system-Python camera dashboards (stdlib only)."""
import os
import ssl
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def server_context(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


def client_context(ca, cert=None, key=None):
    context = ssl.create_default_context(cafile=str(ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if cert or key:
        context.load_cert_chain(cert, key)
    return context


def require_https(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('A HTTPS URL without embedded credentials is required')
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Redirects are disabled for ANCHOR transport')


def open_https(request, *, ca, timeout):
    require_https(request.full_url if isinstance(request, urllib.request.Request) else request)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=client_context(ca)))
    return opener.open(request, timeout=timeout)


def host_tls_files():
    directory = Path(os.environ['ANCHOR_TLS_DIR'])
    return directory / 'ca.crt', directory / 'server.crt', directory / 'server.key'


def secure_server(server, cert, key):
    try:
        server.socket = server_context(cert, key).wrap_socket(server.socket, server_side=True)
    except Exception:
        server.server_close()
        raise
    return server
