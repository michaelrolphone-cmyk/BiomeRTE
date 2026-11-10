#!/usr/bin/env python3
"""Explicit local session control. No session starts automatically.

Examples: --operation open --arguments prepared-session.json
          --operation invite --arguments recipient.json
          --operation state
Resume removes a prior authorization-denied latch only when explicitly invoked.
"""
import argparse
import importlib.util
import json
import os
import sys
from urllib.parse import urlsplit
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from session_service import SessionService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--key', required=True, help='Existing gateway credential file')
    parser.add_argument('--operation', required=True, choices=['state','open','append','finalize','invite','synchronize','recover_open','resume','inspect_reconciliation','reconcile_packet'])
    parser.add_argument('--arguments', help='JSON file containing operation keyword arguments')
    args = parser.parse_args()
    origin = args.origin.rstrip('/')
    parts = urlsplit(origin)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.path or parts.query or parts.fragment:
        parser.error('Expected an HTTPS gateway origin')
    path = os.path.join(os.path.dirname(__file__), 'gateway-agent.py')
    spec = importlib.util.spec_from_file_location('session_gateway_transport', path)
    gateway = spec.loader.load_module('session_gateway_transport')
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
    service = SessionService(args.home, origin, token, access, gateway.fetch)
    result = service.control(args.operation, arguments)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
