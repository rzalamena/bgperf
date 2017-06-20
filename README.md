bgperf
========

This is the DE-CIX enhanced version bgperf, a performance measurement tool for BGP implementations. The aim is to give back as many enhancements as possible to the original bgperf by NTT OSRG.

The enhancements include:
* a more modular way of processing arguments from the commandline
* better metrics collection (variable interval, more data)
* new drop-in replacement for GoBGP 'monitor' implementation using BIRD.
* the action sequencer to wait until convergent and introduce dynamic changes (see UML diagram in /docs)
* selective CPU allocation for all containers (with cpusets)
* support for remote testers in another network (e.g. when you have own tester implementation in your lab)
* support of custom config file for BIRD target (e.g. config produced by IXP toolchain)
* add header to csv output and more metrics when using BIRD based monitor + some tweaks.
* system-specific configuration in settings.py
* new commandline options for most features
* full scenario.yaml support for most features

DE-CIX specific changes that can be removed safely:
* lower mtu on all virtual ethernet adapters (for l2vpn compatibility)

Still ToDo:
* support even more actions in action sequencer
* provide more documentation and usage examples
* port this to recent (2017-06-14) bgperf master branch

## Contents

* [How to install](#how_to_install)
* [How to use](#how_to_use)
* [How bgperf works](https://github.com/osrg/bgperf/blob/master/docs/how_bgperf_works.md)
* [Benchmark remote target](https://github.com/osrg/bgperf/blob/master/docs/benchmark_remote_target.md)

## Prerequisites

* Python 2.7 or later
* Docker

##  <a name="how_to_install">How to install

```bash
$ git clone https://github.com/osrg/bgperf
$ cd bgperf
$ pip install -r pip-requirements.txt
$ ./bgperf.py --help
usage: bgperf.py [-h] [-b BENCH_NAME] [-d DIR]
                 {doctor,prepare,update,bench,config} ...

BGP performance measuring tool

positional arguments:
  {doctor,prepare,update,bench,config}
    doctor              check env
    prepare             prepare env
    update              pull bgp docker images
    bench               run benchmarks
    config              generate config

optional arguments:
  -h, --help            show this help message and exit
  -b BENCH_NAME, --bench-name BENCH_NAME
  -d DIR, --dir DIR
$ ./bgperf.py prepare
$ ./bgperf.py doctor
docker version ... ok (1.9.1)
bgperf image ... ok
gobgp image ... ok
bird image ... ok
quagga image ... ok
```

## external tools required

For metrics collection, or to perform certain actions this version of bgperf depends on external tools to be present.
These are:
* cpupower (part of linux-tools-generic package in Debian/Ubuntu)
* “comcast” (https://github.com/tylertreat/comcast) a wrapper for tc and iptables written in Go.

please install the tools on the system bgperf is executed. You need to configure paths

## <a name="how_to_use">How to use

Use `bench` command to start benchmark test.
By default, `bgperf` benchmarks [GoBGP](https://github.com/osrg/gobgp).
`bgperf` boots 100 BGP test peers each advertises 100 routes to `GoBGP`.

```bash
$ sudo ./bgperf.py bench
run tester
tester booting.. (100/100)
run gobgp
elapsed: 16sec, cpu: 0.20%, mem: 580.90MB
elapsed time: 11sec
```

To change a target implementation, use `-t` option.
Currently, `bgperf` supports [BIRD](http://bird.network.cz/) and [Quagga](http://www.nongnu.org/quagga/)
other than GoBGP.

```bash
$ sudo ./bgperf.py bench -t bird
run tester
tester booting.. (100/100)
run bird
elapsed: 16sec, cpu: 0.00%, mem: 147.55MB
elapsed time: 11sec
$ sudo ./bgperf.py bench -t quagga
run tester
tester booting.. (100/100)
run quagga
elapsed: 33sec, cpu: 0.02%, mem: 477.93MB
elapsed time: 28sec
```

To change a load, use following options.

* `-n` : the number of BGP test peer (default 100)
* `-p` : the number of prefix each peer advertise (default 100)
* `-a` : the number of as-path filter (default 0)
* `-e` : the number of prefix-list filter (default 0)
* `-c` : the number of community-list filter (default 0)
* `-x` : the number of ext-community-list filter (default 0)

```bash
$ sudo ./bgperf.py bench -n 200 -p 50
run tester
tester booting.. (200/200)
run gobgp
elapsed: 23sec, cpu: 0.02%, mem: 1.26GB
elapsed time: 18sec
```
