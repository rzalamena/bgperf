# Copyright (C) 2017 DE-CIX Management GmbH
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

import sys
import datetime
from abc import ABCMeta, abstractmethod, abstractproperty
from collections import deque
from subprocess import call
from base import *

def rm_line():
    print '\x1b[1A\x1b[2K\x1b[1D\x1b[1A'

class Action:
    __metaclass__ = ABCMeta
    type = str()

    @abstractmethod
    def notify(self, data):    # data is a tuple of elapsed, cpu, mem, recved
        pass

    @abstractmethod
    def has_finished(self):
        pass


class WaitConvergentAction(Action):
    def __init__(self, cpu_threshold, routes, confidence, queue, finished):
        self.type = 'wait_convergent'
        self.cpu_threshold = cpu_threshold
        self.routes = routes
        self.history = deque(maxlen = confidence)    # create a fixed size queue to store the last confidence entries
        self.queue = queue
        self.finished = finished
        self.start = datetime.datetime.now()

    def notify(self, data):
        if len(self.history) < self.history.maxlen:
            self.history.appendleft(data)
        else:
            self.history.pop()  # remove one (possibly empty element from the queue)
            self.history.appendleft(data)

    def has_finished(self):
        if len(self.history) == self.history.maxlen and self.has_converged():  # lazy evaluation: has converged should never be called with less than three entries in history
            self.finished.set()
            self.queue.put({"who":"sequencer", "action":"WaitConvergentAction", "message":"Update maximum observed prefixes: {0}".format(self.routes), "prefixes":self.routes})
            elapsed = datetime.datetime.now() - self.start
            print >> sys.stderr, "Action \"wait_convergent\" took {0} seconds".format(elapsed.total_seconds())
            return True
        else:
            return False

    def has_converged(self):
        matches = 0
        for entry in self.history:
            elapsed, cpu, mem, recved = entry
            # DEBUG           print "Checking history entry {0}, {1}, {2}, {3}".format(elapsed, cpu, mem, recved)
            # DEBUG           print "Checking history entry {0}, {1}, {2}, {3}".format(type(elapsed), type(cpu), type(mem), type(recved))
            if cpu <= self.cpu_threshold and recved >= self.routes:
                matches +=1
            else:
                return False
        rm_line()
        print "Found {0} entries in history indicating steady state of bgpd under test".format(matches)
        if matches == self.history.maxlen:
            # DEBUG print "CONVERGENCE DETECTED"
            return True


class SleepAction(Action):
    def __init__(self, duration, finished):   # duration is the minimum guaranteed time to sleep, can be more depending on timing and measurement interval
        self.type = 'sleep'
        self.duration = duration
        self.finished = finished
        self.start = datetime.datetime.now()

    def notify(self, data):
        elapsed, cpu, mem, recved = data
        self.elapsed = elapsed

    def has_finished(self):
        sleep = datetime.datetime.now() - self.start
        print "Action \"sleep\": {0} of {1} seconds elapsed".format(sleep.seconds, self.duration)
        rm_line()

        if sleep >= datetime.timedelta(seconds=self.duration):
            self.finished.set()
            return True
        else:
            return False


class InterruptPeersAction(Action):
    def __init__(self, peers, duration, finished, recovery=0, loss=100): # duration is the minimum guaranteed time to sleep, can be more depending on timing and measurement interval
        self.type = 'interrupt_peers'
        self.duration = duration
        self.recovery = 0 if recovery == None else recovery
        self.loss = 100 if loss == None else loss
        self.finished = finished
        self.start = datetime.datetime.now()
        self.interrupt(peers, loss)

    def notify(self, data):
        pass

    def has_finished(self):
        timespan = datetime.datetime.now() - self.start
        rm_line()
        print "Action \"interrupt_peers\": {0} of {1} seconds elapsed".format(timespan.seconds, self.duration + self.recovery)

        if timespan >= datetime.timedelta(seconds=self.duration + self.recovery):
            self.resume()
            self.finished.set()
            return True
        else:
            # TODO DEBUG remove print"interrupt not finished: {0}".format(timespan)
            return False

    def interrupt(self, peers, loss):
        comcast = '/home/labadm/go/bin/comcast'
        try:
            retcode = call([
            comcast, "--device=eno2", "--packet-loss={0}%".format(loss), \
            "--target-addr={0}".format(",".join(peers)), "--target-proto=tcp"
            ])
            if retcode < 0:
                print >> sys.stderr, "Comcast was terminated by signal", -retcode
            else:
                print >> sys.stderr, "Comcast returned", retcode
        except OSError as e:
            print >>sys.stderr, "Execution failed:", e

    def resume(self):
        comcast = 'comcast' # TODO insert path to comcast here if necessary
        try:
            retcode = call([comcast, "--device=eno2", "--stop"])
            if retcode < 0:
                print >> sys.stderr, "Comcast was terminated by signal", -retcode
            else:
                print >> sys.stderr, "Comcast returned", retcode
        except OSError as e:
            print >>sys.stderr, "Execution failed:", e

class ExecuteProgramAction(Action):
    def __init__(self,path, finished):
        self.type = 'execute'
        self.finished = finished
        self.start = datetime.datetime.now()

    def notify(self, data):
        pass

    def has_finished(self):
        sleep = datetime.datetime.now() - self.start
        print "Action \"execute\": {0} of {1} seconds elapsed".format(sleep.seconds, self.duration)
        rm_line()

        if getoutput(path):#TBD
            self.finished.set()
            return True
        else:
            return False
