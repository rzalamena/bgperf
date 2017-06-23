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
from shutil import copyfile
from settings import cpuset_target

class BIRD(Container):
    def __init__(self, name, host_dir, guest_dir='/root/config', image='bgperf/bird'):
        super(BIRD, self).__init__(name, image, host_dir, guest_dir)

    @classmethod
    def build_image(cls, force=False, tag='bgperf/bird', checkout='HEAD', nocache=False):
        cls.dockerfile = '''
FROM ubuntu:latest
WORKDIR /root
RUN apt-get update && apt-get install -y \
    git autoconf libtool gawk libreadline-dev make flex bison libncurses-dev
RUN git clone --branch v1.6.3 https://gitlab.labs.nic.cz/labs/bird.git bird
RUN cd bird; autoconf && ./configure \
    --enable-client \
    --enable-pthreads \
    --with-protocols="bgp pipe rip ospf static" && \
    make -j2 && make install
'''.format(checkout)
        super(BIRD, cls).build_image(force, tag, nocache)

    def gen_filter_assignment(self, n):
        if 'filter' in n:
            c = []
            if 'in' not in n['filter'] or len(n['filter']['in']) == 0:
                c.append('import all;')
            else:
                c.append('import where {0};'.format('&&'.join(x + '()' for x in n['filter']['in'])))

            if 'out' not in n['filter'] or len(n['filter']['out']) == 0:
                c.append('export all;')
            else:
                c.append('export where {0};'.format('&&'.join(x + '()' for x in n['filter']['out'])))

            return '\n'.join(c)
        return '''import all;
export all;
'''

    def gen_neighbor_config(self, n, conf, add_paths_tx = False, add_paths_rx = False):
        #code.interact(local=locals())
        neighbor_entry = '''table table_{0};

protocol pipe pipe_{0} {{
    table master;
    mode transparent;
    peer table table_{0};
{3}
}}
protocol bgp bgp_{0} {{
    local as {1};
    neighbor {2} as {0};
    table table_{0};
    import all;
    export all;'''.format(n['as'],
                          conf['target']['as'],
                          n['local-address'].split('/')[0],
                          self.gen_filter_assignment(n))

        if 'implementation' in conf['monitor'] and conf['monitor']['implementation'] == 'bird':
            if add_paths_tx:
                neighbor_entry += '\n    add paths tx;'
            if add_paths_rx:
                neighbor_entry += '\n    add paths rx;'

        neighbor_entry += '\n    rs client;\n}'
        # DEBUG print neighbor_entry
        return neighbor_entry


    def scenario2config(self, conf, name='bird.conf'):
        config = '''router id {0};
listen bgp port 179;
log "/var/log/bird.log" all;
protocol device {{ }}
protocol direct {{ disabled; }}
protocol kernel {{ disabled; }}
table master;
'''.format(conf['target']['router-id'])

        def gen_prefix_filter(name, match):
            return '''function {0}()
prefix set prefixes;
{{
prefixes = [
{1}
];
if net ~ prefixes then return false;
return true;
}}
'''.format(name, ',\n'.join(match['value']))

        def gen_aspath_filter(name, match):
            c = '''function {0}()
{{
'''.format(name)
            c += '\n'.join('if (bgp_path ~ [= * {0} * =]) then return false;'.format(v) for v in match['value'])
            c += '''
return true;
}
'''
            return c

        def gen_community_filter(name, match):
            c = '''function {0}()
{{
'''.format(name)
            c += '\n'.join(
                'if ({0}, {1}) ~ bgp_community then return false;'.format(*v.split(':')) for v in match['value'])
            c += '''
return true;
}
'''
            return c

        def gen_ext_community_filter(name, match):
            c = '''function {0}()
{{
'''.format(name)
            c += '\n'.join(
                'if ({0}, {1}, {2}) ~ bgp_ext_community then return false;'.format(*v.split(':')) for v in
                match['value'])
            c += '''
return true;
}
'''
            return c

        def gen_filter(name, match):
            c = ['function {0}()'.format(name), '{']
            for typ, name in match:
                c.append(' if ! {0}() then return false;'.format(name))
            c.append('return true;')
            c.append('}')
            return '\n'.join(c) + '\n'

        # write configuration to file
        with open('{0}/{1}'.format(self.host_dir, name), 'w') as f:
            f.write(config)

            if 'policy' in conf:
                for k, v in conf['policy'].iteritems():
                    match_info = []
                    for i, match in enumerate(v['match']):
                        n = '{0}_match_{1}'.format(k, i)
                        if match['type'] == 'prefix':
                            f.write(gen_prefix_filter(n, match))
                        elif match['type'] == 'as-path':
                            f.write(gen_aspath_filter(n, match))
                        elif match['type'] == 'community':
                            f.write(gen_community_filter(n, match))
                        elif match['type'] == 'ext-community':
                            f.write(gen_ext_community_filter(n, match))
                        match_info.append((match['type'], n))
                    f.write(gen_filter(k, match_info))

            for n in conf['tester']['peers'].values():   # generate config snippets for all testers
                f.write(self.gen_neighbor_config(n, conf))

            # Add monitor section to config
            if 'implementation' in conf['monitor'] and conf ['monitor']['implementation'] == 'bird':
                f.write(self.gen_neighbor_config(conf['monitor'], conf, add_paths_tx=True)) # generate monitor config seperately
            else:
                f.write(self.gen_neighbor_config(conf['monitor'], conf))

            f.flush()
        self.config_name = name
# end scenario2config

    def write_config(self, conf, name='bird.conf'):
        # TODO refactor: make it to a method and bring it to the top level.
        if 'custom-config' in conf['target'] and conf['target']['custom-config']:
            copyfile(conf['target']['custom-config'], '{0}/{1}'.format(self.host_dir, name))
            with open('{0}/{1}'.format(self.host_dir, name), 'a') as f: #append monitor section to custom-target-konfig.
                if 'implementation' in conf['monitor'] and conf ['monitor']['implementation'] == 'bird':
                    f.write(self.gen_neighbor_config(conf['monitor'], conf, add_paths_tx=True)) # generate monitor config seperately
                else:
                    f.write(self.gen_neighbor_config(conf['monitor'], conf))
            self.config_name = name
        else:
            print('generating BIRD config from scenario.yaml')
            self.scenario2config(conf, name='bird.conf')

    def run(self, conf, brname='', cpus=cpuset_target):
        ctn = super(BIRD, self).run(brname, cpus=cpus)

        if self.config_name == None:
            self.write_config(conf)

        startup = '''#!/bin/bash
ulimit -n 65536
mkdir -p /var/log/bird
ip a add {0} dev eth1
bird -c {1}/{2}
'''.format(conf['target']['local-address'], self.guest_dir, self.config_name)
        filename = '{0}/start.sh'.format(self.host_dir)
        with open(filename, 'w') as f:
            f.write(startup)
        os.chmod(filename, 0777)
        i = dckr.exec_create(container=self.name, cmd='{0}/start.sh'.format(self.guest_dir))
        dckr.exec_start(i['Id'], detach=True, socket=True)
        return ctn
