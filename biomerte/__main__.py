"""Usage: python3 -m biomerte --help"""
import argparse
import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
from urllib.error import HTTPError
from .app import Application, initialize, read_json
from .store import Fault, uid, encode
from .identity import sign
from .worker import Worker, Remote, post


def argument(value):
    if value.startswith('@'):
        return read_json(value[1:])
    return json.loads(value)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='biome')
    parser.add_argument('--home', default=os.environ.get('BIOME_HOME',
                        os.path.expanduser('~/.local/share/biomerte')))
    parser.add_argument('--realm', choices=['site', 'local'], default='site')
    subs = parser.add_subparsers(dest='command')
    init = subs.add_parser('init', help='Create a private node identity')
    init.add_argument('--site-id')
    init.add_argument('--etcd', action='append', default=[])
    init.add_argument('--ca')
    init.add_argument('--cert')
    init.add_argument('--key')
    serve = subs.add_parser('serve', help='Start the UI, service bus and BPMN runner')
    serve.add_argument('--listen')
    serve.add_argument('--port', type=int)
    serve.add_argument('--cert')
    serve.add_argument('--key')
    serve.add_argument('--advertise', action='store_true')
    subs.add_parser('token', help='Print this node’s private operator token')
    subs.add_parser('identity', help='Print the public node identity')
    call = subs.add_parser('call', help='Invoke a versioned service operation')
    call.add_argument('operation')
    call.add_argument('arguments', nargs='?', default='{}')
    call.add_argument('--id')
    call.add_argument('--cached', action='store_true')
    subs.add_parser('demo', help='Run, verify and accept a complete work cycle')
    subs.add_parser('discover', help='Discover nearby controllers on this LAN')
    enroll = subs.add_parser('pair', help='Begin or finish physical commissioning')
    enroll.add_argument('url')
    enroll.add_argument('--ca')
    enroll.add_argument('--label', default='Biome provider')
    enroll.add_argument('--finish', action='store_true')
    worker = subs.add_parser('worker', help='Run an enrolled external provider')
    worker.add_argument('manifest')
    worker.add_argument('--once', action='store_true')
    sync = subs.add_parser('sync', help='Exchange revision pages with a trusted peer')
    sync.add_argument('manifest')
    pkg = subs.add_parser(
        'install-package', help='Install reviewed provider code by exact package digest')
    pkg.add_argument('package')
    pkg.add_argument('resource')
    pkg.add_argument('manifest')
    resolve = subs.add_parser(
        'resolve-effect', help='Record an observed result for an uncertain local provider operation')
    resolve.add_argument('manifest')
    resolve.add_argument('operation')
    resolve.add_argument('state', choices=['succeeded', 'failed'])
    resolve.add_argument('result')
    bk = subs.add_parser('backup')
    bk.add_argument('destination')
    rs = subs.add_parser('restore')
    rs.add_argument('source')
    resume = subs.add_parser('resume-restore')
    resume.add_argument('--previous-owner-retired', action='store_true')
    args = parser.parse_args(argv)
    args.home = os.path.abspath(args.home)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == 'restore':
        from .backup import restore
        output = restore(args.source, args.home)
    elif args.command == 'discover':
        from .network import discover
        output = discover(3)
    else:
        fresh = not os.path.isfile(os.path.join(args.home, 'node.json'))
        if args.command not in ('serve', 'init') and fresh:
            raise Fault('Initialize the node first: python3 -m biomerte init')
        if args.command == 'init' and args.etcd and not args.site_id:
            raise Fault('Shared nodes require an explicit --site-id')
        conf = initialize(args.home, getattr(args, 'site_id', None))
        if args.command == 'init' and args.etcd:
            if not fresh:
                raise Fault('Configure shared storage on a new node directory')
            if not args.site_id:
                raise Fault('Shared nodes require an explicit --site-id')
            conf['etcd'] = {'endpoints': args.etcd,
                            'ca': args.ca, 'cert': args.cert, 'key': args.key}
            with open(os.path.join(args.home, 'node.json'), 'w') as f:
                json.dump(conf, f, indent=2)
        if args.command == 'token':
            print(conf['admin_token'])
            return 0
        if args.command in ('identity', 'init'):
            output = {k: conf[k] for k in ('site', 'node', 'public_key')}
            if args.command == 'init':
                output['home'] = args.home
        else:
            hold = os.path.join(args.home, 'RECOVERY_HOLD')
            if args.command == 'resume-restore':
                if not args.previous_owner_retired:
                    raise Fault('Confirm previous owner retirement and device reconciliation')
                if os.path.isfile(hold):
                    os.unlink(hold)
                output = {'ready': True}
            elif os.path.exists(hold):
                raise Fault('Restored node is on recovery hold. See RECOVERY_HOLD.')
            else:
                app = Application(args.home)
                if args.command == 'serve':
                    from .web import server, background
                    host = args.listen or conf['listen']
                    port = args.port if args.port is not None else conf['port']
                    http = server(app, host, port, args.cert, args.key)
                    stop = threading.Event()
                    threads = []
                    for realm in ('local', 'site'):
                        th = threading.Thread(target=background, args=(app, stop, realm))
                        th.daemon = True
                        th.start()
                        threads.append(th)
                    if args.advertise:
                        from .network import respond
                        th2 = threading.Thread(target=respond, args=(
                            app, stop, http.server_port, bool(args.cert)))
                        th2.daemon = True
                        th2.start()

                    def shutdown(signum, frame):
                        threading.Thread(target=http.shutdown).start()
                    signal.signal(signal.SIGTERM, shutdown)
                    print('BiomeRTE at %s://%s:%d' %
                          ('https' if args.cert else 'http', host, http.server_port))
                    sys.stdout.flush()
                    try:
                        http.serve_forever()
                    except KeyboardInterrupt:
                        pass
                    finally:
                        stop.set()
                        http.server_close()
                        for thread in threads:
                            thread.join(10)
                    return 0
                elif args.command == 'call':
                    output = app.call(args.operation, argument(args.arguments),
                                      args.id or uid(), args.realm, cached=args.cached)
                elif args.command == 'demo':
                    ids = app.call('demo.run', {}, uid(), args.realm)
                    for unused in range(100):
                        app.tick(args.realm)
                        r = app.call('process.get', {'run': ids['run']}, realm=args.realm)
                        if r['state'] != 'running':
                            break
                    if r['state'] != 'completed':
                        raise Fault('Demo did not complete: ' + str(r.get('result')))
                    item = next(w for w in app.call('work.list', realm=args.realm)
                                if w['id'] == ids['task'])
                    accepted = app.call('work.accept', {'id': item['id'], 'revision': item['revision'],
                                                        'evidence': {'stored result': r['result']['revision']}}, uid(), args.realm)
                    output = dict(ids, process_state=r['state'],
                                  task_state=accepted['state'], evidence=r['result'])
                elif args.command == 'install-package':
                    from .package_install import install
                    output = install(app, args.package, args.resource, args.manifest)
                elif args.command == 'backup':
                    from .backup import backup
                    output = backup(app, args.destination)
                elif args.command == 'pair':
                    pending = os.path.join(args.home, 'pair-pending.json')
                    if not args.finish:
                        answer = post(args.url, '/v1/pair/begin',
                                      {'public_key': conf['public_key'], 'label': args.label}, args.ca)
                        if answer['key'] != app.node:
                            raise Fault('Peer did not return the expected device key')
                        with open(pending, 'w') as f:
                            json.dump({'url': args.url, 'challenge': answer}, f)
                        output = {
                            'fingerprint': app.node, 'controller_site': answer['site'], 'next': 'Verify fingerprint at the controller, approve scopes, then run pair --finish.'}
                    else:
                        data = read_json(pending)
                        if data['url'] != args.url:
                            raise Fault('Pending pairing targets another URL')
                        proof = dict(data['challenge'], signature=sign(app.key, data['challenge']))
                        output = post(args.url, '/v1/pair/finish', proof, args.ca)
                elif args.command in ('worker', 'sync', 'resolve-effect'):
                    cfg = read_json(args.manifest)
                    remote = Remote(cfg['url'], cfg['site'], app.node, app.key,
                                    cfg.get('ca'), realm=cfg.get('realm', 'site'),
                                    controller=cfg.get('controller'))
                    if args.command == 'resolve-effect':
                        from .worker import reconcile_receipt
                        output = reconcile_receipt(
                            args.home, remote, args.operation, args.state, argument(args.result))
                    elif args.command == 'sync':
                        counts = app.call('sync.cursors', realm=args.realm)
                        total = 0
                        while True:
                            packet = remote.call(
                                'sync.export', {'to': app.node, 'cursors': counts, 'limit': cfg.get('page', 16)})
                            receipt = app.call('sync.import', {'packet': packet}, uid(), args.realm)
                            counts = receipt['cursors']
                            total += receipt['applied']
                            if not receipt['more']:
                                break
                        output = {'imported': total, 'cursors': counts}
                    else:
                        from .worker import run_worker
                        return run_worker(args.home, remote, cfg, args.once)
    print(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    try:
        sys.exit(main())
    except HTTPError as exc:
        sys.stderr.write('HTTP %d: %s\n' % (exc.code, exc.read(2048).decode('utf-8', 'replace')))
        sys.exit(1)
    except (Fault, OSError, ValueError, TypeError, KeyError) as exc:
        sys.stderr.write(str(exc) + '\n')
        sys.exit(1)
