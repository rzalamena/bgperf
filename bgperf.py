#!/usr/bin/env python
#
# Copyright (C) 2015, 2016 Nippon Telegraph and Telephone Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import yaml
import time
import shutil
import netaddr
import signal
import datetime
import code
import glob
import subprocess
from docker import Client
from argparse import ArgumentParser, REMAINDER
from itertools import chain, islice
from requests.exceptions import ConnectionError
from pyroute2 import IPRoute
from socket import AF_INET
from nsenter import Namespace
from base import *
from exabgp import ExaBGP
from gobgp import GoBGP
from bird import BIRD
from quagga import Quagga
from tester import Tester
from monitor import Monitor
from birdmonitor import BirdMonitor
from settings import dckr
import settings
from Queue import Queue
from subprocess import call

def rm_line():
    print '\x1b[1A\x1b[2K\x1b[1D\x1b[1A'


def gc_thresh3():
    gc_thresh3 = '/proc/sys/net/ipv4/neigh/default/gc_thresh3'
    with open(gc_thresh3) as f:
        return int(f.read().strip())


def doctor(args):
    ver = dckr.version()['Version']
    ok = int(''.join(ver.split('.'))) >= 190
    print 'docker version ... {1} ({0})'.format(ver, 'ok' if ok else 'update to 1.9.0 at least')

    print 'bgperf image',
    if img_exists('bgperf/exabgp'):
        print '... ok'
    else:
        print '... not found. run `bgperf prepare`'

    for name in ['gobgp', 'bird', 'quagga']:
        print '{0} image'.format(name),
        if img_exists('bgperf/{0}'.format(name)):
            print '... ok'
        else:
            print '... not found. if you want to bench {0}, run `bgperf prepare`'.format(name)

    print '/proc/sys/net/ipv4/neigh/default/gc_thresh3 ... {0}'.format(gc_thresh3())


def prepare(args):
    ExaBGP.build_image(args.force, nocache=args.no_cache)
    GoBGP.build_image(args.force, nocache=args.no_cache)
    Quagga.build_image(args.force, nocache=args.no_cache)
    BIRD.build_image(args.force, nocache=args.no_cache)


def update(args):
    if args.image == 'all' or args.image == 'exabgp':
        ExaBGP.build_image(True, checkout=args.checkout, nocache=args.no_cache)
    if args.image == 'all' or args.image == 'gobgp':
        GoBGP.build_image(True, checkout=args.checkout, nocache=args.no_cache)
    if args.image == 'all' or args.image == 'quagga':
        Quagga.build_image(True, checkout=args.checkout, nocache=args.no_cache)
    if args.image == 'all' or args.image == 'bird':
        BIRD.build_image(True, checkout=args.checkout, nocache=args.no_cache)
    if args.image == 'all' or args.image == 'monitorbird':
        BirdMonitor.build_image(True, checkout=args.checkout, nocache=args.no_cache)


def bench(args):
    config_dir = '{0}/{1}'.format(args.dir, args.bench_name)
    brname = args.bench_name + '-br'

    ip = IPRoute()
    ctn_intfs = flatten((l.get_attr('IFLA_IFNAME') for l in ip.get_links() if l.get_attr('IFLA_MASTER') == br) for br in ip.link_lookup(ifname=brname))

    if not args.repeat:
        # currently ctn name is same as ctn intf
        # TODO support proper mapping between ctn name and intf name
        for ctn in ctn_intfs:   # remove all existing containers with a matching name (initial run)
            dckr.remove_container(ctn, force=True) if ctn_exists(ctn) else None

        if os.path.exists(config_dir):  # ensure configuration dir is empty
            shutil.rmtree(config_dir)
    else:   # repetition of a prior benchmark: remove all containers but tester
        for ctn in ctn_intfs:
            if ctn != 'tester':
                dckr.remove_container(ctn, force=True) if ctn_exists(ctn) else None

    if not os.path.exists(config_dir): # ensure config dir exists
        os.makedirs(config_dir)

    if args.file:
        with open(args.file) as f:
            conf = yaml.load(f)
    else:   # no config file given on the commandline
        conf = gen_conf(args)

    script2config(args, conf)
    with open('{0}/scenario.yaml'.format(config_dir), 'w') as f:    # write backup
            f.write(yaml.dump(conf))

    if len(conf['tester']['peers']) > gc_thresh3():
        print 'gc_thresh3({0}) is lower than the number of peer({1})'.format(gc_thresh3(), len(conf['tester']['peers']))
        print 'type next to increase the value'
        print '$ echo 16384 | sudo tee /proc/sys/net/ipv4/neigh/default/gc_thresh3'

    if args.target == 'gobgp':
        target = GoBGP
    elif args.target == 'bird':
        target = BIRD
    elif args.target == 'quagga':
        target = Quagga

    bird_monitor = args.bird_monitor or conf['monitor']['implementation'] == 'bird'
    is_target_remote = True if 'remote' in conf['target'] and conf['target']['remote'] == 'true' else False

    if is_target_remote:
        r = ip.get_routes(dst=conf['target']['local-address'].split('/')[0], family=AF_INET)
        if len(r) == 0:
            print 'no route to remote target {0}'.format(conf['target']['local-address'])
            sys.exit(1)

        idx = [t[1] for t in r[0]['attrs'] if t[0] == 'RTA_OIF'][0]
        intf = ip.get_links(idx)[0]

        if intf.get_attr('IFLA_MASTER') not in ip.link_lookup(ifname=brname):
            br = ip.link_lookup(ifname=brname)
            if len(br) == 0:
                ip.link_create(ifname=brname, kind='bridge')
                br = ip.link_lookup(ifname=brname)
            br = br[0]
            ip.link('set', index=idx, master=br)
    else:
        print 'run', args.target
        if args.image:
            target = target(args.target, '{0}/{1}'.format(config_dir, args.target), image=args.image)
        else:
            target = target(args.target, '{0}/{1}'.format(config_dir, args.target))
        target.run(conf, brname)

    if args.bird_monitor or conf['monitor']['implementation'] == 'bird':
        print 'run Bird monitor'
        m = BirdMonitor('birdmonitor', config_dir+'/monitor')
    else:
        print 'run monitor'
        m = Monitor('monitor', config_dir+'/monitor')
    m.run(conf, brname)

    time.sleep(1)

    print 'waiting bgp connection between {0} and monitor'.format(args.target)

    if args.bird_monitor or conf['monitor']['implementation'] == 'bird':
        m.wait_established(conf['target']['as'])

    is_tester_remote = True if 'remote-address' in conf['tester'] and conf['tester']['remote-address'] else False

    if not args.repeat:
        print 'run tester'
        t = Tester('tester', config_dir+'/tester')
        t.run(conf, brname)
    else:
        print 'Not (re-)starting local tester container'
        print 'Launching AWS/Docker based external tester with fixed number of peers' # TODO make number of peers configurable
        #call(["php", "bgpdocker/test.php"]) # launch peers while not connected to benchmark network

    if is_tester_remote:
        r = ip.get_routes(dst=conf['tester']['remote-address'].split('/')[0], family=AF_INET)
        if len(r) == 0:
            print 'no route to remote tester(s) {0}'.format(conf['tester']['remote-address'])
            sys.exit(1)

        idx = [t[1] for t in r[0]['attrs'] if t[0] == 'RTA_OIF'][0]
        intf = ip.get_links(idx)[0]

        if intf.get_attr('IFLA_MASTER') not in ip.link_lookup(ifname=brname):
            # FIXME prints none print 'Inteface {0} is *not* in bridge {1}, correcting'.format(intf.get_attr('IFLA_MASTER'), brname)
            br = ip.link_lookup(ifname=brname)
            if len(br) == 0:
                print 'bridge {0} was nonexistant before, adding'.format(brname)
                ip.link_create(ifname=brname, kind='bridge', mtu=1446)
                br = ip.link_lookup(ifname=brname)
            br = br[0]
            ip.link('set', index=idx, master=br, mtu=1446) # setting master attribute

    start = datetime.datetime.now()

    q = Queue()

    if 'script' in conf and len(conf['script']) > 0:
        sequencer = Sequencer(conf['script'],start, q)
    else:
        sequencer = None

    m.stats(q)
    if not is_target_remote:
        target.stats(q)

    def mem_human(v):
        if v > 1000 * 1000 * 1000:
            return '{0:.2f}GB'.format(float(v) / (1000 * 1000 * 1000))
        elif v > 1000 * 1000:
            return '{0:.2f}MB'.format(float(v) / (1000 * 1000))
        elif v > 1000:
            return '{0:.2f}KB'.format(float(v) / 1000)
        else:
            return '{0:.2f}B'.format(float(v))

    if args.output == 'config_dir':
        f = open('{0}/output_{1}.csv'.format(config_dir, args.bench_name), 'w')
    else:
        f = open(args.output, 'w') if args.output else None

    if f:    # add header to CSV file : "elapsed time (s.mmm), cpu, mem, recvd, prefix_delta"
        f.write('elapsed, cpu, mem, nets, recvd, delta, time')
        if target.cpus:
            for cpu in target.cpus: f.write(", cpufreq_{0}".format(cpu))
        f.write('\n')
        f.flush()

    cpu = 0
    mem = 0
    cpufreqs = []
    prefix_delta = 0
    max_prefixes = 0
    expected_prefixes = 0
    cooling = -1
    if sequencer: sequencer.start()

    def sigint_handler(signum, frame):
        teardown()
        sys.exit(130) # int 0 as return code means successfull termination. int 130 see http://www.tldp.org/LDP/abs/html/exitcodes.html#EXITCODESREF

    signal.signal(signal.SIGINT, sigint_handler)

    while True:
        info = q.get()

        if not is_target_remote and info['who'] == target.name:
            cpu = info['cpu']
            mem = info['mem']
            cpufreqs = info['cpufreqs'] if 'cpufreqs' in info and len(info['cpufreqs']) > 0 else []


        if info['who'] == m.name:
            now = datetime.datetime.now()
            nowstring = '{:%Y-%m-%d %H:%M:%S}'.format(now)
            elapsed = now - start

            if bird_monitor:
                recved = int(info['state']['routes-matching']) if 'routes-matching' in info['state'] else 0
                networks = int(info['state']['unique-networks']) if 'unique-networks' in info['state'] else 0
            else:
                recved = info['state']['adj-table']['accepted'] if 'accepted' in info['state']['adj-table'] else 0
                networks = 0 # GoBGP based monitor implementation cannot obtain this info.

            if max_prefixes < recved:   # update max_prefixes from observation
                max_prefixes = recved

            if elapsed.seconds > 0:
                rm_line()
            if expected_prefixes > 0:
                prefix_delta = expected_prefixes - recved

            print 'now: {0}, elapsed: {1} sec, cpu: {2:>4.2f}%, mem: {3}, routes: {4}, max_prefixes: {5}, delta: {6}'.format(nowstring, elapsed.total_seconds(), cpu, mem_human(mem), recved, max_prefixes, prefix_delta)
            if prefix_delta < 0:
                print "WARNING: negative prefix delta indicating inaccurate (e.g. too low) number of routes given in WaitConvergentAction!"
            if sequencer: sequencer.notify((elapsed, cpu, mem, recved)) # TODO pass delta to sequencer?

            if f:# write statistics
                f.write('{0}, {1}, {2}, {3}, {4}, {5}, {6}'.format(elapsed.total_seconds(), cpu, mem, networks, recved, prefix_delta, nowstring))
                for freq in cpufreqs: f.write(", {0}".format(freq[1]))
                f.write('\n')
                f.flush()

            if cooling == args.cooling:
                f.close() if f else None
                return

            if cooling >= 0:
                cooling += 1

            if info['checked']:
                cooling = 0

        if info['who'] == 'sequencer': # accept input from sequencer
            print info['message']
            if 'action' in info and info['action'] == 'WaitConvergentAction':
                expected_prefixes = info['prefixes'] # update the expected number of prefixes

def gen_conf(args):
    neighbor = args.neighbor_num
    prefix = args.prefix_num
    as_path_list = args.as_path_list_num
    prefix_list = args.prefix_list_num
    community_list = args.community_list_num
    ext_community_list = args.ext_community_list_num

    conf = {}
    conf['target'] = {
        'as': args.target_ASN,
        'router-id': '10.10.0.1',
        'local-address': '10.10.0.1/16',
        'remote': 'true' if args.target_remote else '', # only empty strings evaluate to false!
        'custom-config': args.target_custom_konfig if args.target_custom_konfig else '',
    }

    conf['monitor'] = {
        'implementation': 'bird' if args.bird_monitor else 'gobgp',
        'as': 1001,
        'router-id': '10.10.0.2',
        'local-address': '10.10.0.2/16',
        'check-points': [prefix * neighbor],
    }

    conf['tester'] = {
        'remote-address': args.tester_remote_address,
        'peers': {},
        #FIXME remove 'remote': 'true' if args.remote_tester else '',
    }
    offset = 0  #FIXME unused -> git blame

    it = netaddr.iter_iprange('90.0.0.0', '100.0.0.0')

    conf['policy'] = {}

    assignment = []

    if prefix_list > 0:
        name = 'p1'
        conf['policy'][name] = {
            'match': [{
                'type': 'prefix',
                'value': list('{0}/32'.format(ip) for ip in islice(it, prefix_list)),
            }],
        }
        assignment.append(name)

    if as_path_list > 0:
        name = 'p2'
        conf['policy'][name] = {
            'match': [{
                'type': 'as-path',
                'value': list(range(10000, 10000 + as_path_list)),
            }],
        }
        assignment.append(name)

    if community_list > 0:
        name = 'p3'
        conf['policy'][name] = {
            'match': [{
                'type': 'community',
                'value': list('{0}:{1}'.format(i/(1<<16), i%(1<<16)) for i in range(community_list)),
            }],
        }
        assignment.append(name)

    if ext_community_list > 0:
        name = 'p4'
        conf['policy'][name] = {
            'match': [{
                'type': 'ext-community',
                'value': list('rt:{0}:{1}'.format(i/(1<<16), i%(1<<16)) for i in range(ext_community_list)),
            }],
        }
        assignment.append(name)

    # generate neighbors for tester section, skip if all neighbors are remote
    it = netaddr.iter_iprange('100.0.0.0','160.0.0.0')
    if args.tester_remote_address:
        #print 'EXPERIMENTAL expecting remote tester with bgp neighbors at IPv4 addresses:' #FIXME remove EXPERIMENTAL tag
        #asns = [9063, 34966, 50469, 37468, 39090, 5539]
        #for i in range(1, neighbor+1):  # support DE-CIX style peering lan
        #    print '172.31.{0}.{1}'.format(193+(i/255), (i%255)+99)
        #    # FIXME this populates the peers section in scenario
        #    router_id = '172.31.{0}.{1}'.format(193+(i/255), (i%255)+99)
        #    conf['tester']['peers'][router_id] = {
        #        'as': asns[i-1],
        #        'router-id': router_id,
        #        'local-address': router_id + '/20',
        #        'paths': list('{0}/32'.format(ip) for ip in islice(it, prefix)),
        #        'filter': {
        #            args.filter_type: assignment,
        #        },
        #    }

        return conf
    else:
        for i in range(3, neighbor+3):
            router_id = '10.10.{0}.{1}'.format(i/255, i%255)
            conf['tester']['peers'][router_id] = {
                'as': 1000 + i,
                'router-id': router_id,
                'local-address': router_id + '/20',
                'paths': list('{0}/32'.format(ip) for ip in islice(it, prefix)),
                'filter': {
                    args.filter_type: assignment,
                },
            }
        return conf

def script2config(args, conf):
    if args.script:
        print("select action script benchmark type: execute scripted actions during benchmark")
        print("processing action script file: {0}".format(args.script))
        with open(args.script) as asf:
            script = yaml.load(asf)['script']
        conf['script']=script
    else:
        print("select generic benchmark type: terminate on checkpoint")


def config(args):
    conf = gen_conf(args)
    script2config(args, conf)   # add script to global config

    with open(args.output, 'w') as f:   # write config to file
        f.write(yaml.dump(conf))

# teardown everything setup for one benchmark run, e.g. when user pressed CTRL + C
def teardown():
        c = Client(base_url='unix://var/run/docker.sock')
        print 'stoping bird container..'
        c.stop('bird')  # stop the bird container

        print 'stoping monitor container..'
        c.stop('monitor') #stop the monitor container
        #c.remove_container('monitor')#remove the monitor container
        #print([x.get_attr('IFLA_IFNAME') for x in ip.get_links()])
        ipr = IPRoute()
        # lookup the index
        dev = ipr.link_lookup(ifname='bgperf-br')[0]
        # bring it down
        ipr.link('set', index=dev, state='down')
        #print (os.listdir("/tmp"))
        print 'removing temp files...'
        #for fl in glob.glob("/tmp/*.tmp"):
        #        os.remove(fl)
        #print (os.listdir("/tmp"))

        #TODO uncomment if necessary ok, output = getoutput(['comcast', '--device=eno2', '--stop'])
        print (ok,output)

def cleanup(args): # remove possibly every trace of bgperf from the system
        teardown(args) # start by calling cleanup

        # TODO do this in a for-loop.
        c = Client(base_url='unix://var/run/docker.sock')
        print 'removing bird container..'
        c.remove_container('bird') #remove the bird container
        print 'removing monitor container..'
        c.remove_container('monitor') #remove the monitor container
        print 'removing bird container..'
        c.remove_container('tester') #remove the tester container


if __name__ == '__main__':
    parser = ArgumentParser(description='BGP performance measuring tool')
    parser.add_argument('-b', '--bench-name', default='bgperf')
    parser.add_argument('-d', '--dir', default='/tmp')
    s = parser.add_subparsers(title="actions") # s holds the subparsers for every action
    parser_doctor = s.add_parser('doctor', help='check env')
    parser_doctor.set_defaults(func=doctor)

    parser_prepare = s.add_parser('prepare', help='prepare env')
    parser_prepare.add_argument('-f', '--force', action='store_true', help='build even if the container already exists')
    parser_prepare.add_argument('-n', '--no-cache', action='store_true')
    parser_prepare.set_defaults(func=prepare)

    parser_update = s.add_parser('update', help='rebuild bgp docker images')
    parser_update.add_argument('image', choices=['exabgp', 'gobgp', 'bird', 'monitorbird', 'quagga', 'all'])
    parser_update.add_argument('-c', '--checkout', default='HEAD')
    parser_update.add_argument('-n', '--no-cache', action='store_true')
    parser_update.set_defaults(func=update)

    # parent parser for arguments common to bench and config, necassary because bench also calls gen_config()
    parser_parent_bench_config = ArgumentParser(add_help=False)
    parser_parent_bench_config.add_argument('-n', '--neighbor-num', default=100, type=int)
    parser_parent_bench_config.add_argument('-p', '--prefix-num', default=100, type=int)
    parser_parent_bench_config.add_argument('-l', '--filter-type', choices=['in', 'out'], default='in')
    parser_parent_bench_config.add_argument('-a', '--as-path-list-num', default=0, type=int)
    parser_parent_bench_config.add_argument('-e', '--prefix-list-num', default=0, type=int)
    parser_parent_bench_config.add_argument('-c', '--community-list-num', default=0, type=int)
    parser_parent_bench_config.add_argument('-x', '--ext-community-list-num', default=0, type=int)
    parser_parent_bench_config.add_argument('--tester-remote-address', default='', type=str, help='EXPERIMENTAL specify remote network address of tester(s) in CIDR notation to replaces *all* neighbors. Example \"172.31.2.0/24\"')
    parser_parent_bench_config.add_argument('--target-remote', action='store_true', help='generate a config with remote target (bgpd) as described in docs/benchmark_remote_target.md')
    parser_parent_bench_config.add_argument('--target-ASN', default=1000, type=int, help='the Autonomous System Number (ASN) to be used for the target bgpd implementation')
    parser_parent_bench_config.add_argument('-k', '--target-custom-konfig', metavar='TARGET_CONFIG_FILE', help='override the configuration file of the target bgpd. Use this instead of the generated one. EXPERIMENTAL currently supported for target=bird/bird_mt') # misspelling of config as konfig is intendet to give a hint to the user for single letter parameter -k
    parser_parent_bench_config.add_argument('-m', '--measurement-interval', default=1, type=int, help='reporting interval (in seconds) of the statistics collected by monitor (stdout and file)')
    parser_parent_bench_config.add_argument('-s', '--script', metavar='ACTION SCRIPT_FILE', help='action script file is included scenario.yaml and saved to output folder. The contents of ACTION SCRIPT FILE take precedence over any script present in CONFIG FILE.')
    parser_parent_bench_config.add_argument('-y', '--bird-monitor', action='store_true', help='use alternative BIRD monitor implementation for satistics collection')

    parser_bench = s.add_parser('bench', parents=[parser_parent_bench_config], help='run benchmarks')
    parser_bench.add_argument('-t', '--target', choices=['gobgp', 'bird', 'quagga'], default='gobgp')
    parser_bench.add_argument('-i', '--image', help='specify custom docker image')
    parser_bench.add_argument('-r', '--repeat', action='store_true', help='use existing tester/monitor container')
    parser_bench.add_argument('-f', '--file', metavar='CONFIG_FILE')
    parser_bench.add_argument('-g', '--cooling', default=0, type=int)
    parser_bench.add_argument('-o', '--output', metavar='STAT_FILE', help='special value \"config_dir\" generates output to the config directory in a file named output_BENCH_NAME.csv')
    parser_bench.add_argument('--tester-cpus', type=str, default=settings.cpuset_tester, help='Override cpuset-cpus of tester container, default \"{0}\" (from settings.py)'.format(settings.cpuset_tester))
    parser_bench.add_argument('--target-cpus', type=str, default=settings.cpuset_target, help='Override cpuset-cpus of target container, default \"{0}\" (from settings.py)'.format(settings.cpuset_target))
    parser_bench.add_argument('--monitor-cpus', type=str, default=settings.cpuset_monitor, help='Override cpuset-cpus of monitor container, default \"{0}\" (from settings.py)'.format(settings.cpuset_monitor))
    parser_bench.set_defaults(func=bench)

    parser_config = s.add_parser('config', parents=[parser_parent_bench_config], help='generate config')
    parser_config.add_argument('-o', '--output', default='bgperf.yml', type=str)
    parser_config.set_defaults(func=config)

    parser_teardown = s.add_parser('teardown', help='teardown of benchmark run')
    parser_config.set_defaults(func=teardown)

    parser_cleanup = s.add_parser('cleanup', help='cleanup of the system before removing bgperf')
    parser_config.set_defaults(func=cleanup)

    args = parser.parse_args()
    args.func(args)
