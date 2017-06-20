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

from base import *
import os
from  settings import dckr
import yaml
import json
from threading import Thread
import time
import StringIO
import itertools
import code

class BirdMonitor(Container):
    def __init__(self, name, host_dir, guest_dir='/root/config', image='bgperf/monitorbird'):
        super(BirdMonitor, self).__init__(name, image, host_dir, guest_dir)
        self.config = None

    @classmethod
    def build_image(cls, force=False, tag='bgperf/monitorbird', checkout='v1.6.3', nocache=False): # Use v1.6.3 as the latest stable version
        cls.dockerfile = '''
FROM ubuntu:latest
WORKDIR /root
RUN apt-get update && apt-get install -qy git autoconf libtool gawk make \
flex bison libncurses-dev libreadline6-dev iputils-ping
RUN apt-get install -qy flex
RUN git clone https://gitlab.labs.nic.cz/labs/bird.git bird && \
(cd bird && git checkout {0} && autoconf && ./configure && make && make install)
'''.format(checkout)
        super(BirdMonitor, cls).build_image(force, tag, nocache)

    def write_config(self, conf, name='bird_monitor.conf'):
        config = '''log syslog all;
log "/var/log/bird/bird.log" all;
log stderr all;
debug commands 1;

router id {0};
listen bgp address {1} port 179;

protocol device {{
}}

protocol direct {{
  disabled;
}}

protocol kernel {{
  disabled;
}}

protocol bgp bgp_{2} {{
    local as {3};
    neighbor {4} as {2};
    import all;
    export none;
    add paths rx;
}}
'''.format(conf['monitor']['router-id'],
           conf['monitor']['local-address'].split('/')[0],
           conf['target']['as'],
           conf['monitor']['as'],
           conf['target']['local-address'].split('/')[0]
          )

        with open('{0}/{1}'.format(self.host_dir, name), 'w') as f:
            f.write(config)
            f.flush
        self.config_name = name


    def run(self, conf, brname=''):
        ctn = super(BirdMonitor, self).run(brname)
        print "created BIRD monitor container"

        if self.config_name == None:
            self.write_config(conf)

        startup = '''#!/bin/bash
ulimit -n 65536
ip a add {0} dev eth1
mkdir -p /var/log/bird
bird -c {1}/{2}
'''.format(conf['monitor']['local-address'], self.guest_dir, self.config_name)

        filename = '{0}/start.sh'.format(self.host_dir)
        with open(filename, 'w') as f:
            f.write(startup)
        os.chmod(filename, 0777)
        i = dckr.exec_create(container=self.name, cmd='{0}/start.sh'.format(self.guest_dir))
        dckr.exec_start(i['Id'], detach=True, socket=True)
        self.config = conf
        return ctn

    def local(self, cmd, stream=False): # run an arbitrary command inside the container and receive it's output stream
        i = dckr.exec_create(container=self.name, cmd=cmd)
        return dckr.exec_start(i['Id'], stream=stream)

    def wait_established(self, target_as): # poll monitor bird if the session to the bgpd under test is established
        while True:
            stream = self.local('birdc show protocol bgp_{0}'.format(target_as))
            #print stream # TODO extract to method
            buf = StringIO.StringIO(stream)
            buf.readline() # Skip first line similar to "BIRD 1.6.3 ready."
            header = buf.readline().split() # read line simlar to "name     proto    table    state  since       info"
            elements = buf.readline().split() # read line similar to "bgp_65534 BGP      table_65534 up     2017-03-01 16:00:30  Established"
            neigh = dict(zip(header, elements))
            if neigh['info'] == 'Established':
                return
            time.sleep(1)

    def stats(self, queue):
        def stats():
            cps = self.config['monitor']['check-points'] if 'check-points' in self.config['monitor'] else []
            while True:
                info = {}
                info ['who'] = self.name

                stream = self.local('birdc show route count')
                buf = StringIO.StringIO(stream)
                buf.readline() # Skip first line similar to "BIRD 1.6.3 ready."
                elements = buf.readline().split() # read line similar to "0 of 0 routes for 0 networks"
                info ['state'] = {}
                info ['state'] ['routes-matching'] = elements[0]
                info ['state'] ['routes-all'] = elements[2]
                info ['state'] ['unique-networks'] = elements[5]

                info['who'] = self.name
                state = info['state']

                if 'routes-matching' in state and len(cps) > 0 and int(cps[0]) == int(state['routes-matching']):
                    cps.pop(0)
                    info['checked'] = True
                else:
                    info['checked'] = False

                queue.put(info)
                time.sleep(1)

        t = Thread(target=stats)
        t.daemon = True
        t.start()
