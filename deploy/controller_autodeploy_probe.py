#!/usr/bin/env python3
"""Read-only process-source and authenticated registration probes (Python 3.4+)."""
from __future__ import print_function
import argparse
import json
import os
import re
import subprocess
import sys
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlparse

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, message, headers, newurl):
        return None

def read_json(path):
    with open(path, encoding='utf-8') as stream:
        return json.load(stream)

def output(argv):
    return subprocess.check_output(argv, stderr=subprocess.DEVNULL, timeout=15).decode('utf-8')

def source(config, release):
    expected = os.path.realpath(release)
    service = config['service']
    if service['manager'] == 'systemd':
        text = output(['systemctl', 'show', service['unit'], '-p', 'MainPID'])
        pid = int(text.strip().split('=', 1)[1])
        if pid <= 0:
            raise ValueError('controller_not_running')
        cwd = os.path.realpath(os.readlink('/proc/{}/cwd'.format(pid)))
        # The stable launcher pins the process cwd to its physical release.
        # Do not read cmdline/environ: proc process-memory reads can page in
        # controller memory and block behind an unhealthy device's disk I/O.
        valid = cwd == expected
    elif service['manager'] == 'launchd':
        target = service.get('domain', 'gui/' + str(os.getuid())) + '/' + service['label']
        match = re.search(r'^\s*pid\s*=\s*(\d+)\s*$', output(['/bin/launchctl', 'print', target]), re.M)
        if not match:
            raise ValueError('controller_not_running')
        pid = int(match.group(1))
        records = output(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'])
        names = [line[1:] for line in records.splitlines() if line.startswith('n')]
        cwd = os.path.realpath(names[0]) if len(names) == 1 else None
        valid = cwd == expected
    else:
        raise ValueError('unsupported_service_manager')
    if not valid:
        raise ValueError('controller_source_mismatch')
    return {'check': 'process_source', 'pid': pid, 'source': expected}

def gateway(config):
    options = config['gateway']
    origin = options['origin'].rstrip('/')
    parsed = urlparse(origin)
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        raise ValueError('https_gateway_required')
    identity = read_json(os.path.join(config['home'], 'node.json'))
    with open(options['key_file'], encoding='utf-8') as stream:
        token = stream.read().strip()
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
               'User-Agent': 'BiomeRTE-Autodeploy-Registration/1'}
    access_path = options.get('site_access_file')
    if access_path and os.path.isfile(access_path):
        access = read_json(access_path)
        if access.get('origin') != origin:
            raise ValueError('site_access_origin_mismatch')
        headers['OAI-Sites-Authorization'] = 'Bearer ' + access['token']
    request = Request(origin + '/bridge/ha/status', data=b'{}', headers=headers)
    with build_opener(NoRedirect).open(request, timeout=options.get('timeout', 15)) as response:
        value = json.loads(response.read().decode('utf-8'))
    if value.get('site') != identity['site']:
        raise ValueError('gateway_site_mismatch')
    matches = [row for row in value.get('nodes', []) if row.get('id') == options['node']]
    if len(matches) != 1:
        raise ValueError('gateway_node_missing')
    node = matches[0]
    age = time.time() - float(node.get('seen') or 0)
    if (not node.get('enabled') or not node.get('ready')
            or node.get('host') != os.uname().nodename
            or age < -30 or age > options.get('freshness_seconds', 60)):
        raise ValueError('gateway_node_not_ready')
    # This proves authenticated registration, not a controller RPC round trip.
    result = {'check': 'gateway_registration', 'node': node['id'], 'age_seconds': round(age, 2)}
    if options.get('require_network', False):
        network = node.get('network')
        report = network.get('self_report') if isinstance(network, dict) else None
        if (not isinstance(report, dict) or report.get('current') is not True
                or report.get('status') != 'current'
                or not isinstance(report.get('addresses'), list)):
            raise ValueError('gateway_network_report_not_current')
        result['network_report_current'] = True
    return result

def link_preflight(config, release):
    """Import the staged gateway entrypoint without starting services or I/O."""
    physical = os.path.realpath(release)
    if (not os.path.isfile(os.path.join(physical, 'deploy', 'gateway-agent.py'))
            or not os.path.isfile(os.path.join(physical, 'biomerte', 'network_addresses.py'))):
        raise ValueError('gateway_link_source_missing')
    program = (
        "import os,runpy,sys; "
        "root=os.path.realpath(sys.argv[1]); "
        "sys.path.insert(0,os.path.join(root,'deploy')); "
        "runpy.run_path(os.path.join(root,'deploy','gateway-agent.py'),"
        "run_name='biomerte_gateway_preflight'); "
        "modules=('local_link','ha_link','native_call','session_service',"
        "'persistent_link','controller_inbox','provider_ingress',"
        "'dashboard_proxy','grok_receivers','checkpoint_maintenance','websocket'); "
        "[__import__(name) for name in modules]"
    )
    environment = dict(os.environ)
    environment['PYTHONPATH'] = physical
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    subprocess.check_output([config['python'], '-S', '-B', '-c', program, physical],
                            cwd=physical, env=environment, stderr=subprocess.DEVNULL,
                            timeout=config.get('preflight_timeout', 120))
    return {'check': 'gateway_link_imports', 'source': physical}


def deployment_gateway(config, release=None):
    """Optional paired-service proof; old installations keep their prior gate."""
    services = config.get('linked_services', [])
    if not isinstance(services, list):
        raise ValueError('linked_services_must_be_list')
    if services and not release:
        raise ValueError('linked_service_release_required')
    proofs = [source({'service': service}, release) for service in services]
    result = gateway(config)
    if proofs:
        result['linked_sources'] = proofs
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--mode', choices=['source', 'gateway', 'link-preflight'], required=True)
    parser.add_argument('--release')
    options = parser.parse_args(argv)
    config = read_json(options.config)
    if options.mode in ('source', 'link-preflight') and not options.release:
        parser.error('--release is required for a source or link preflight probe')
    if options.mode == 'source':
        value = source(config, options.release)
    elif options.mode == 'link-preflight':
        value = link_preflight(config, options.release)
    else:
        value = deployment_gateway(config, options.release)
    print(json.dumps(value, sort_keys=True))
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        # Never expose server bodies, credentials, process environments or URLs.
        code = str(error) if isinstance(error, ValueError) else type(error).__name__
        sys.stderr.write(code + '\n')
        sys.exit(1)
