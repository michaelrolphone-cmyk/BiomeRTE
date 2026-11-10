#!/usr/bin/env python3
"""Explicit local responsibility control; never starts a transfer automatically.

Run outside the business scheduler, since preparing a handoff fences business
writes. Use existing registered-controller credentials. Exact package/snapshot
arguments must be reviewed before any bootstrap, freeze or authority expansion.
"""
import argparse
import importlib.util
import json
import os
import sys
from urllib.parse import urlsplit
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from biomerte.app import Application
from biomerte.ownership import OwnershipRuntime
from biomerte.store import Fault
from urllib.error import HTTPError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--key', required=True, help='Existing registered-controller credential file')
    parser.add_argument('--operation', required=True, choices=['state','bootstrap','adopt_current','prepare','package','stage','commit','cancel','acknowledge_cancelled','reconcile','resume'])
    parser.add_argument('--arguments', help='Reviewed JSON keyword arguments')
    args = parser.parse_args()
    origin = args.origin.rstrip('/')
    parts = urlsplit(origin)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.path or parts.query or parts.fragment:
        parser.error('Expected an HTTPS gateway origin')
    path = os.path.join(os.path.dirname(__file__), 'gateway-agent.py')
    spec = importlib.util.spec_from_file_location('ownership_gateway_transport', path)
    gateway = spec.loader.load_module('ownership_gateway_transport')
    token = gateway.read_secret(args.key)
    access = None
    access_path = os.path.join(os.path.dirname(args.key), 'site-access.json')
    if os.path.exists(access_path):
        with open(access_path) as handle:
            record = json.load(handle)
        if record.get('origin') != origin:
            parser.error('Private Site access belongs to another origin')
        access = record['token']
    arguments = {}
    if args.arguments:
        with open(args.arguments) as handle:
            arguments = json.load(handle)
    def remote(operation, payload):
        try:
            return gateway.fetch(origin + '/bridge/ha/ownership/' + operation,
                                 payload, token, timeout=45, site_access=access)
        except HTTPError as error:
            raise Fault('Ownership gateway HTTP ' + str(error.code), error.code)
    runtime = OwnershipRuntime(Application(args.home), remote)
    result = getattr(runtime, args.operation)(**arguments)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
