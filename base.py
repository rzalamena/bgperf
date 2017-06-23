# Copyright (C) 2016 Nippon Telegraph and Telephone Corporation.
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

from settings import dckr
import io
import os
import yaml
import sys
import subprocess
import warnings
import StringIO
from pyroute2 import IPRoute
from itertools import chain
from nsenter import Namespace
from threading import Thread
from threading import Event
from datetime import timedelta
from actions import WaitConvergentAction, SleepAction, InterruptPeersAction

flatten = lambda l: chain.from_iterable(l)

def ctn_exists(name):
    return '/{0}'.format(name) in list(flatten(n['Names'] for n in dckr.containers(all=True)))


def img_exists(name):
    return name in [ctn['RepoTags'][0].split(':')[0] for ctn in dckr.images() if ctn['RepoTags'] != None]

def getoutput(cmd, successful_status=(0,), stacklevel=1):
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        output, _ = p.communicate()
        status = p.returncode
    except EnvironmentError as e:
        warnings.warn(str(e), UserWarning, stacklevel=stacklevel)
        return False, ''
    if os.WIFEXITED(status) and os.WEXITSTATUS(status) in successful_status:
        return True, output
    return False, output


# Parses output in this format of cpupower
#              |Mperf
#PKG |CORE|CPU | C0   | Cx   | Freq
#   0|   0|   0|  0.00| 100.0|4400
#   2|   0|   1|  0.00| 100.0|3874

# TODO Design conflict, the measured values are only valid for global measurement interval = 1 sec.
def get_turbo_clks():
    info = []
    ok, output = getoutput(['cpupower', 'monitor', '-mMperf'])
    output = StringIO.StringIO(output) # convert to StringIO for later line-based parsing
    if ok:
        output.readline() # skip fist line
        names = tuple(s.strip() for s in output.readline().split('|')) # names tuple
        for line in output: # read values
            values = (s.strip() for s in line.split('|'))
            info.append(dict(zip(names, values)))
    else:
        print("Failed to execute \"cpupower\" please install it on your system.")
        print output
    return info

# This method is limited to obtain the maximum Base Clk but not the turbo Clk
def get_cpuinfo():
    info = [{}]
    ok, output = getoutput(['uname', '-m'])
    if ok:
        info[0]['uname_m'] = output.strip()
    try:
        fo = open('/proc/cpuinfo')
    except EnvironmentError as e:
        warnings.warn(str(e), UserWarning)
    else:
        for line in fo:
            name_value = [s.strip() for s in line.split(':', 1)]
            if len(name_value) != 2:
                continue
            name, value = name_value
            if not info or name in info[-1]:  # next processor
                info.append({})
            info[-1][name] = value
        fo.close()
        return info



class docker_netns(object):
    def __init__(self, name):
        pid = int(dckr.inspect_container(name)['State']['Pid'])
        if pid == 0:
            raise Exception('no container named {0}'.format(name))
        self.pid = pid

    def __enter__(self):
        pid = self.pid
        if not os.path.exists('/var/run/netns'):
            os.mkdir('/var/run/netns')
        os.symlink('/proc/{0}/ns/net'.format(pid), '/var/run/netns/{0}'.format(pid))
        return str(pid)

    def __exit__(self, type, value, traceback):
        pid = self.pid
        os.unlink('/var/run/netns/{0}'.format(pid))


def connect_ctn_to_br(ctn, brname):
    print 'connecting container {0} to bridge {1}'.format(ctn, brname)
    with docker_netns(ctn) as pid:
        ip = IPRoute()
        br = ip.link_lookup(ifname=brname)
        if len(br) == 0:
            ip.link_create(ifname=brname, kind='bridge', mtu=1446)
            br = ip.link_lookup(ifname=brname)
        br = br[0]
        ip.link('set', index=br, state='up', mtu=1446)

        ifs = ip.link_lookup(ifname=ctn)
        if len(ifs) > 0:
           ip.link_remove(ifs[0])

        ip.link_create(ifname=ctn, kind='veth', peer=pid, mtu=1446)
        host = ip.link_lookup(ifname=ctn)[0]
        ip.link('set', index=host, master=br)
        ip.link('set', index=host, state='up')
        guest = ip.link_lookup(ifname=pid)[0]
        ip.link('set', index=guest, net_ns_fd=pid)
        with Namespace(pid, 'net'):
            ip = IPRoute()
            ip.link('set', index=guest, ifname='eth1', mtu=1446)
            ip.link('set', index=guest, state='up')

class Sequencer(Thread):
    # script: the script to execute
    # benchmark_start: start time of the benchmark this sequencer is part of
    # queue: the "main" queue of the benchmark which is responsible for logging and output of measured data to STDOUT
    def __init__(self, script, benchmark_start, queue):
        Thread.__init__(self)
        self.daemon = True
        self.name = 'sequencer'

        self.script = script            # the script is a list of benchmark actions
        self.benchmark_start = benchmark_start    # start time of the benchmark run
        self.queue = queue

        self.elapsed = timedelta(0)
        self.action = None              # the currently running action

    def run(self):
        print "\033[1;32;47mstarting Sequenecer\033[1;30;47m"
        while len(self.script) > 0:     # simple sequencer, execute actions one at a time
            action = self.script.pop(0)['action']
            info = {}
            info['who'] = self.name
            info['message'] = "\nAction \"{0}\" started at {1}".format(action['type'], self.elapsed.total_seconds())
            # DEBUG           print "Action \"{0}\" started at {1}".format(action['type'], self.elapsed.total_seconds()) # FIXME Debug remove
            self.queue.put(info)
            if self.execute_action(action):
                info['message'] = "\033[1;32;47mAction \"{0}\" finished at {1}\033[1;30;47m".format(action['type'], self.elapsed.total_seconds())
            else:
                info['message'] = "\033[1;31;47mAction \"{0}\" FAILED at {1}\033[1;30;47m".format(action['type'], self.elapsed.total_seconds())
            self.queue.put(info)
        print "Sequencer: script finished!"


    # halts until action is finished and returns True when execution finished successfullly.
    def execute_action(self, a):
        finished = Event()
        while True:
            if a['type'] == 'wait_convergent':
                self.action = WaitConvergentAction(a['cpu_below'], a['routes'], a['confidence'], self.queue, finished)
            elif a['type'] == 'interrupt_peers':
                recovery = a['recovery'] if 'recovery' in a and a['recovery'] else None
                loss = a['loss'] if 'loss' in a and a['loss'] else None
                self.action = InterruptPeersAction(a['peers'], a['duration'], finished, recovery, loss)
            elif a['type'] == 'sleep':
                self.action = SleepAction(a['duration'], finished)
            elif a['type'] =='execute':
                self.action = ExecuteProgramAction(a['path'],finished)
            else:
                print "ERROR: unrecognized action of type {0}".format(a['type'])
                return False # return error here

            finished.wait()
            finished.clear()
            break

        return True

    def notify(self, data):    # elapsed is the ammount of time since the beginning of the action
        elapsed, cpu, mem, recved = data
        self.elapsed = elapsed
        if self.action:
            self.action.notify(data)
            self.action.has_finished()
        else:
            print >>sys.stderr, "Call .notify() on None object"


class Container(object):
    def __init__(self, name, image, host_dir, guest_dir):
        self.name = name
        self.image = image
        self.host_dir = host_dir
        self.guest_dir = guest_dir
        self.config_name = None
        if not os.path.exists(host_dir):
            os.makedirs(host_dir)
            os.chmod(host_dir, 0777)
        self.cpuset_cpus = None
        self.cpus = None # list of integers containing every core id


    @classmethod
    def build_image(cls, force, tag, nocache=False):
        def insert_after_from(dockerfile, line):
            lines = dockerfile.split('\n')
            i = -1
            for idx, l in enumerate(lines):
                elems = [e.strip() for e in l.split()]
                if len(elems) > 0 and elems[0] == 'FROM':
                    i = idx
            if i < 0:
                raise Exception('no FROM statement')
            lines.insert(i+1, line)
            return '\n'.join(lines)

        for env in ['http_proxy', 'https_proxy']:
            if env in os.environ:
                cls.dockerfile = insert_after_from(cls.dockerfile, 'ENV {0} {1}'.format(env, os.environ[env]))

        f = io.BytesIO(cls.dockerfile.encode('utf-8'))
        if force or not img_exists(tag):
            print 'build {0}...'.format(tag)
            for line in dckr.build(fileobj=f, rm=True, tag=tag, decode=True, nocache=nocache):
                if 'stream' in line:
                    print line['stream'].strip()


    def run(self, brname='', rm=True, cpus=''):
        if rm and ctn_exists(self.name):
            print 'remove container:', self.name
            dckr.remove_container(self.name, force=True)

        config = dckr.create_host_config(binds=['{0}:{1}'.format(os.path.abspath(self.host_dir), self.guest_dir)],
                                         privileged=True)
        ctn = dckr.create_container(image=self.image, command='bash', detach=True, name=self.name,
                                    stdin_open=True, volumes=[self.guest_dir], host_config=config)
        if cpus:
            print('running container {0} with non-default cpuset: {1}'.format(self.name, cpus))
            dckr.update_container(container=self.name, cpuset_cpus=cpus)
            self.cpuset_cpus = cpus
            # parse into list of integers for later use
            ranges = (x.split("-") for x in cpus.split(","))
            self.cpus = [i for r in ranges for i in range(int(r[0]), int(r[-1]) + 1)]
        dckr.start(container=self.name)
        if brname != '':
            connect_ctn_to_br(self.name, brname)
        self.ctn_id = ctn['Id']

        return ctn

    def stats(self, queue):

        def stats():
            for stat in dckr.stats(self.ctn_id, decode=True):
                cpu_percentage = 0.0
                prev_cpu = stat['precpu_stats']['cpu_usage']['total_usage']
                try:
                    prev_system = stat['precpu_stats']['system_cpu_usage']
                except KeyError:
                    prev_system = 0
                cpu = stat['cpu_stats']['cpu_usage']['total_usage']
                system = stat['cpu_stats']['system_cpu_usage']
                cpu_num = len(stat['cpu_stats']['cpu_usage']['percpu_usage'])
                cpu_delta = float(cpu) - float(prev_cpu)
                system_delta = float(system) - float(prev_system)
                if system_delta > 0.0 and cpu_delta > 0.0:
                    cpu_percentage = (cpu_delta / system_delta) * float(cpu_num) * 100.0
                # collect core speed (MHz) of cpus where the process is running (if cpuset is used)
                if self.cpus:
                    cpufreqs = [] # put the current corespeeds for all cpus in cpuset in a list
                    cpuinfo = get_turbo_clks()
                    #cpuinfo = get_cpuinfo()
                    for cpu in self.cpus:
                        speed = cpuinfo[cpu]['Freq']
                        #speed = cpuinfo[cpu]['cpu MHz']
                        cpufreqs.append((cpu, speed)) # build a list of tuples with cpu_id, speed
                    queue.put({'who': self.name, 'cpu': cpu_percentage, 'mem': stat['memory_stats']['usage'], 'cpufreqs': cpufreqs})
                else:
                    queue.put({'who': self.name, 'cpu': cpu_percentage, 'mem': stat['memory_stats']['usage']})

        t = Thread(target=stats)
        t.daemon = True
        t.start()
