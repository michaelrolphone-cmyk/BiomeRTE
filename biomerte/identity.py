"""Node-local key material. OpenSSL is used only for pairing and signed exchange."""
import binascii
import hashlib
import os
import subprocess
import tempfile
from .store import Fault, encode, b64, un64


def secret():
    return binascii.hexlify(os.urandom(32)).decode('ascii')


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def openssl(argv, data=b''):
    p = subprocess.Popen(['openssl'] + argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    out, err = p.communicate(data)
    if p.returncode:
        raise Fault('OpenSSL operation failed')
    return out


def generate(path):
    key = openssl(['genrsa', '2048'])
    write_private(path, key)
    return openssl(['rsa', '-pubout'], key).decode('ascii')


def fingerprint(pem):
    if not isinstance(pem,str) or len(pem)>8192 or 'BEGIN PUBLIC KEY' not in pem:
        raise Fault('Expected PEM public key')
    der = openssl(['rsa','-pubin','-outform','DER','-pubout'],pem.encode('ascii'))
    return hashlib.sha256(der).hexdigest()


def sign(key, payload):
    return b64(openssl(['dgst','-sha256','-sign',key],encode(payload).encode()))


def verify(pem, signature, payload):
    with tempfile.NamedTemporaryFile() as pub, tempfile.NamedTemporaryFile() as sig:
        pub.write(pem.encode('ascii'));pub.flush()
        sig.write(un64(signature));sig.flush()
        openssl(['dgst','-sha256','-verify',pub.name,'-signature',sig.name],encode(payload).encode())


def signed(key, payload):
    return {'payload':payload,'signature':sign(key,payload)}
