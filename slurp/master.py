"""
"""
from collections import defaultdict
import errno
import fcntl
import logging
from itertools import cycle
import os
import select
try:
    from setproctitle import setproctitle
except ImportError:
    setproctitle = None
import signal
import sys
import threading
import time

from conf import load as load_conf
from channel import create_channels
import sink


logger = logging.getLogger(__name__)


SIGNALS = dict(map(
    lambda x: (getattr(signal, 'SIG%s' % x), x),
    'HUP QUIT INT TERM USR1 USR2 WINCH CHLD'.split()
    ))


NONE = object()


class Worker(object):
    """
    """

    class MasterCheckThread(threading.Thread):

        def __init__(self, parent_pid):
            super(Worker.MasterCheckThread, self).__init__()
            self.parent_pid = parent_pid
            self.check_freq = 1.0
            self.daemon = True

        def run(self):
            while True:
                if self.parent_pid != os.getppid():
                    logger.warning('parent %s changed to %s, terminating',
                        self.parent_pid, os.getppid())
                    os.kill(os.getpid(), signal.SIGTERM)
                    break
                time.sleep(self.check_freq)

    class Terminate(Exception):
        pass

    def __init__(self, channels, tracking_file, op):
        self.channels = channels
        self.tracking_file = tracking_file
        self.op = op
        self.terminated = False

    @property
    def signal_map(self):
        return {
            signal.SIGTERM: self.handle_term,
            }

    def handle_term(self, signum, frame):
        logger.info('terminating')
        self.terminated = True
        raise self.Terminate()

    def run(self, paths):
        try:
            channels = create_channels(self.channels)
            while True:
                self.op(
                    channels=channels,
                    paths=paths,
                    tracking=self.tracking_file,
                    callback=lambda: self.terminated)
        except self.Terminate:
            pass
        except Exception, ex:
            logger.exception(ex)
            raise



class Master(object):
    """
    Used to manage forked workers.

    See https://github.com/benoitc/gunicorn/blob/master/gunicorn/arbiter.py.
    """

    class Terminate(Exception):
        pass

    class Worker(object):

        def __init__(self, name, channels, pid=None, fail_count=0):
            self.name = name
            self.channels = channels
            self.pid = pid
            self.fail_count = fail_count

        def kill(self, sig):
            logger.info('sending signal "%s" to %s worker %s',
                SIGNALS[sig], self.name, self.pid)
            try:
                os.kill(self.pid, sig)
            except OSError, e:
                if e.errno != errno.ESRCH:
                    raise
                self.pid = None

    def __init__(self,
            conf_file,
            worker_count,
            op,
            channel_includes=None,
            channel_excludes=None,
            backfill=NONE,
            tracking_file=NONE,
            sink=NONE):
        self.conf_file = conf_file
        if worker_count < 1:
            raise ValueError('Worker count {} is < 1'.format(worker_count))
        self.worker_count = worker_count
        self.channel_includes = channel_includes
        self.channel_excludes = channel_excludes
        self.backfill = backfill
        self.tracking_file = tracking_file
        self.sink = sink
        self.op = op
        self.conf = None
        self.name_to_channel = None
        self.max_fail_count = 10
        self.max_signal_queue = 10
        self.stop_timeout = 10
        self.reload_timeout = 10
        self.signals = []
        self.workers = {}
        self.pipe_r, self.pipe_w = None, None

    def _load_conf(self):
        conf = load_conf(
            self.conf_file,
            self.channel_includes,
            self.channel_excludes)
        if self.tracking_file is not NONE:
            conf['tracking_file'] = self.tracking_file
        tagger = cycle(map(str, range(self.worker_count)))
        for channel in conf['channels']:
            if channel['tag'] is None:
                channel['tag'] = tagger.next()
            if self.backfill is not NONE:
                channel['backfill'] = self.backfill
            if self.sink is not NONE:
                channel['sink'] = self.sink
        channels = defaultdict(list)
        for channel in conf['channels']:
            channels[channel['tag']].append(channel)
        self.channels = channels
        self.conf = conf
        return self.conf

    @property
    def signal_map(self):
        return {
            signal.SIGHUP: self.on_signal,
            signal.SIGTERM: self.on_signal,
            }

    def on_signal(self, signum, frame):
        logger.debug('signal num - "%s", frame - %s', signum, frame)
        if len(self.signals) < self.max_signal_queue:
            self.signals.append(signum)
            self.wakeup()
        else:
            logger.warning(
                'signal queue %s > %s dropping signal num - "%s", frame - %s',
                len(self.signals), self.max_signal_queue, signum, frame)

    def handle_term(self):
        raise self.Terminate()

    def handle_hup(self):
        self.reload()

    def __call__(self, paths):
        # title
        if setproctitle:
            setproctitle('slurp: master')

        # conf
        logger.info('loading configuration "%s"', self.conf_file)
        self._load_conf()

        # pipe for waking up during sleep (e.g. when we get a signal)
        self.pipe_r, self.pipe_w = os.pipe()
        for fd in [self.pipe_r, self.pipe_w]:
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            flags |= fcntl.FD_CLOEXEC
            fcntl.fcntl(fd, fcntl.F_SETFD, flags)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK

        # loop
        try:
            while True:
                self.manage_workers(paths)
                sig = self.signals.pop(0) if self.signals else None
                if not sig:
                    self.sleep()
                    continue
                sig_name = SIGNALS[sig]
                sig_handler = getattr(self, 'handle_' + sig_name.lower(), None)
                if not sig_handler:
                    logger.warning('ignoring unhandled signal "%s"', sig_name)
                    continue
                logger.info('handling signal "%s"', sig_name)
                sig_handler()
        except self.Terminate:
            pass
        except Exception, ex:
            logger.exception(ex)
            raise
        finally:
            self.stop()

    def sleep(self):
        try:
            ready = select.select([self.pipe_r], [], [], 1.0)
            if not ready[0]:
                return
            while os.read(self.pipe_r, 1):
                pass
        except select.error, e:
            if e[0] not in [errno.EAGAIN, errno.EINTR]:
                raise
        except OSError, e:
            if e.errno not in [errno.EAGAIN, errno.EINTR]:
                raise
        except KeyboardInterrupt:
            sys.exit()

    def wakeup(self):
        try:
            os.write(self.pipe_w, '.')
        except IOError, e:
            if e.errno not in [errno.EAGAIN, errno.EINTR]:
                raise

    def stop(self):
        count = 0
        expires = time.time() + self.stop_timeout
        while time.time() < expires:
            count = self.kill_workers(signal.SIGTERM)
            if count == 0:
                break
            time.sleep(0.1)
            self.reap_workers()
        if count != 0:
            self.kill_workers(signal.SIGKILL)

    def manage_workers(self, paths):
        # fill in missing workers
        missing = set(self.channels.keys()).difference(self.workers.keys())
        for name in missing:
            channels = self.channels[name]
            self.workers[name] = self.Worker(name=name, channels=channels)

        # kill obsolete workers
        obsolete = set(self.workers.keys()).difference(self.channels.keys())
        for name in obsolete:
            worker = self.workers[name]
            if worker.pid is None:
                self.workers.pop(worker.name)
            else:
                if worker.fail_count > self.max_fail_count:
                    worker.kill(signal.SIGKILL)
                else:
                    worker.kill(signal.SIGTERM)

        # reap dead workers
        self.reap_workers()

        # spawn workers
        for worker in self.workers.itervalues():
            if (worker.pid is None and
                worker.fail_count < self.max_fail_count):
                self.spawn_worker(worker, paths)

    def spawn_worker(self, worker, paths):
        # fork
        pid = os.fork()
        if pid != 0:
            # parent
            worker.pid = pid
            return

        # child
        try:
            # clear signals
            for sig in SIGNALS.iterkeys():
                signal.signal(sig, signal.SIG_DFL)

            # title
            if setproctitle:
                setproctitle('slurp: worker[{}]'.format(worker.name))

            # loop
            worker = Worker(
                worker.channels,
                self.conf['tracking_file'],
                self.op)
            for sig, handler in worker.signal_map.iteritems():
                signal.signal(sig, handler)
            worker.run(paths)
        except Exception, ex:
            logger.exception(ex)
            os._exit(1)
        os._exit(0)

    def reap_workers(self):
        try:
            while True:
                worker_pid, status = os.waitpid(-1, os.WNOHANG)
                if not worker_pid:
                    break
                exit_code = status >> 8
                worker = [
                    worker for worker in self.workers.itervalues()
                    if worker.pid == worker_pid
                    ]
                if not worker:
                    logger.warning('unknown worker %s', worker_pid)
                    continue
                worker = worker[0]
                if exit_code != 0:
                    logger.warning('%s worker %s failed with exit code %s',
                        worker.name, worker.pid, exit_code)
                    worker.fail_count += 1
                    if worker.fail_count >= self.max_fail_count:
                        logger.error(
                            '%s worker fail count %s >= max fail count %s',
                            worker.name, worker.fail_count, self.max_fail_count)
                else:
                    logger.info('%s worker %s exited', worker.name, worker.pid)
                worker.pid = None
        except OSError, e:
            if e.errno == errno.ECHILD:
                return
            raise

    def kill_workers(self, sig):
        count = 0
        logger.info('sending signal "%s" to all workers', SIGNALS[sig])
        for worker in self.workers.itervalues():
            if worker.pid is not None:
                worker.kill(sig)
                count += 1
        return count

    def reload(self):
        logger.info('reloading configuration "%s"', self.conf_path)
        self._load_conf()
        logger.info('reloading')
        expires = time.time() + self.reload_timeout
        while self.workers and time.time() < expires:
            self.kill_workers(signal.SIGTERM)
            time.sleep(0.1)
            self.reap_workers()
        self.kill_workers(signal.SIGKILL)
        for worker in self.workers:
            worker.fail_count = 0