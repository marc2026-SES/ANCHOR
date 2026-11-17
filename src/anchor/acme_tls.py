"""TLS adapter for pinned ACME 283bd7d; leaves the upstream checkout unchanged.

ACME conflates outgoing certificate verification with incoming mTLS and disables
MQTT hostname verification. This entrypoint separates those concerns. HTTPS uses
server authentication + existing oneM2M ACP; MQTT additionally requires client TLS
certificates. Source guards fail closed if the supported upstream code changes.
"""
import hashlib
import importlib
import os
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

import paho.mqtt.client as mqtt
import requests

from .tls import client_context, host_tls_files, require_https, server_context

GUARDS = {
    'acmecse.services.SecurityManager': '78c38d270794dd13c6ecd3fa4e670dfb6d9b5171e1a680e52e8f17d8a9c05524',
    'acmecse.helpers.MQTTConnection': '2dae4e2690afc97d57ed82cb06e20794d92134c1443723620a1fd2ef87091850',
    'acmecse.plugins.bindings.HttpServer': '41370a12e924cd32bca8cb8441f146a7c6429824f5fae70cfee28a22d62aa2b6',
    'acmecse.plugins.bindings.MQTTClient': '2d5abec1a831f6e4535c3406dbeb8a5010f95f8a969f0be972491a89b26f36e4',
}
_installed = False


class VerifiedMQTTClient(mqtt.Client):
    def connect_async(self, *args, **kwargs):
        if self._ssl_context is None:
            raise ValueError('MQTT connections require TLS')
        return super().connect_async(*args, **kwargs)

    def tls_set(self, ca_certs=None, certfile=None, keyfile=None, **kwargs):
        ca, cert, key = host_tls_files()
        self.tls_set_context(client_context(ca_certs or ca, certfile or cert, keyfile or key))

    def tls_insecure_set(self, value):
        # Upstream always passes True. Never permit it to weaken this context.
        super().tls_insecure_set(False)


def verified_request(method, url, **kwargs):
    require_https(url)
    ca, _, _ = host_tls_files()
    kwargs.update(verify=str(ca), allow_redirects=False)
    with requests.Session() as session:
        session.trust_env = False
        response = session.request(method, url, **kwargs)
        if 300 <= response.status_code < 400:
            raise requests.exceptions.SSLError('CSE redirects are disabled')
        return response


def install_adapter():
    global _installed
    if _installed:
        return
    from acmecse.runtime import CSE  # establish ACME's normal import order
    from acmecse.runtime.Configuration import Configuration
    for name, expected in GUARDS.items():
        module = importlib.import_module(name)
        if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'Unsupported ACME source: {name}; review TLS adapter before upgrading')
    security = importlib.import_module('acmecse.services.SecurityManager')
    connection = importlib.import_module('acmecse.helpers.MQTTConnection')
    http = importlib.import_module('acmecse.plugins.bindings.HttpServer')
    mqtt_binding = importlib.import_module('acmecse.plugins.bindings.MQTTClient')
    from acmecse.etc.Constants import RuntimeConstants as RC

    def https_context(self):
        if not Configuration.http_security_useTLS or not Configuration.http_security_verifyCertificate:
            raise ValueError('ANCHOR CSE requires useTLS=true and verifyCertificate=true')
        return server_context(Configuration.http_security_caCertificateFile,
                              Configuration.http_security_caPrivateKeyFile)

    security.SecurityManager.getSSLContextHttp = https_context
    connection.MQTTClient = VerifiedMQTTClient
    original_validate = mqtt_binding.MQTTClient.validate

    @wraps(original_validate)
    def validate_mqtt(self, config):
        if config.mqtt_enable and not (config.mqtt_security_useTLS and config.mqtt_security_verifyCertificate):
            raise ValueError('ANCHOR MQTT binding requires TLS and certificate verification')
        original_validate(self, config)
        if config.mqtt_enable:
            # Upstream advertises mqtt:// even for TLS, often with a loopback broker.
            # Public CSE POA uses the same reachable host as its HTTPS endpoint.
            host = urlsplit(config.http_address).hostname
            if not host:
                raise ValueError('CSE HTTPS address requires a host')
            if ':' in host:
                host = '[' + host + ']'
            old = f'mqtt://{config.mqtt_address}:{config.mqtt_port}'
            RC.csePOA[:] = [f'mqtts://{host}:{config.mqtt_port}' if value == old else value for value in RC.csePOA]

    mqtt_binding.MQTTClient.validate = validate_mqtt
    # Bindings retain the original requests functions in this dispatch table.
    for operation, method in http.HttpServer.operation2method.items():
        http.HttpServer.operation2method[operation] = (
            lambda url, _method=method.__name__.upper(), **kwargs: verified_request(_method, url, **kwargs))
    _installed = True


def main():
    ca, cert, key = host_tls_files()
    client_context(ca, cert, key)  # fail before starting listeners when provisioning is missing
    os.environ['REQUESTS_CA_BUNDLE'] = str(ca)
    install_adapter()
    from acmecse.__main__ import main as acme_main
    acme_main()


if __name__ == '__main__':
    main()
