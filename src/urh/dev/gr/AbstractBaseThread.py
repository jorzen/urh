import os
import socket
import sys
import tempfile
from queue import Queue, Empty
from subprocess import Popen, PIPE
from threading import Thread

import time

import zmq
from PyQt5.QtCore import QThread, pyqtSignal

from urh import constants
from urh.util.Logger import logger

ON_POSIX = 'posix' in sys.builtin_module_names


class AbstractBaseThread(QThread):
    started = pyqtSignal()
    stopped = pyqtSignal()
    sender_needs_restart = pyqtSignal()

    def __init__(self, sample_rate, freq, gain, bandwidth, receiving: bool,
                 ip='127.0.0.1', parent=None):
        # setting parent to None here, as setting parent to Dialog may lead to crashes described in
        # https://github.com/jopohl/urh/issues/163
        super().__init__(None)
        self.ip = ip
        self.port = 1337
        self._sample_rate = sample_rate
        self._freq = freq
        self._gain = gain
        self._bandwidth = bandwidth
        self._receiving = receiving  # False for Sender-Thread
        self.usrp_ip = "192.168.10.2"
        self.device = "USRP"
        self.current_index = 0

        self.context = None
        self.socket = None

        if constants.SETTINGS.value("use_gnuradio_install_dir", False, bool):
            gnuradio_dir = constants.SETTINGS.value("gnuradio_install_dir", "")
            with open(os.path.join(tempfile.gettempdir(), "gnuradio_path.txt"), "w") as f:
                f.write(gnuradio_dir)
            self.python2_interpreter = os.path.join(gnuradio_dir, "gr-python27", "python.exe")
        else:
            self.python2_interpreter = constants.SETTINGS.value("python2_exe", "")

        self.queue = Queue()
        self.data = None  # Placeholder for SenderThread
        self.current_iteration = 0  # Counts number of Sendings in SenderThread

        self.tb_process = None

    @property
    def sample_rate(self):
        return self._sample_rate

    @sample_rate.setter
    def sample_rate(self, value):
        self._sample_rate = value
        if self.tb_process:
            try:
                self.tb_process.stdin.write(b'SR:' + bytes(str(value), "utf8") + b'\n')
                self.tb_process.stdin.flush()
            except BrokenPipeError:
                pass

    @property
    def freq(self):
        return self._freq

    @freq.setter
    def freq(self, value):
        self._freq = value
        if self.tb_process:
            try:
                self.tb_process.stdin.write(b'F:' + bytes(str(value), "utf8") + b'\n')
                self.tb_process.stdin.flush()
            except BrokenPipeError:
                pass

    @property
    def gain(self):
        return self._gain

    @gain.setter
    def gain(self, value):
        self._gain = value
        if self.tb_process:
            try:
                self.tb_process.stdin.write(b'G:' + bytes(str(value), "utf8") + b'\n')
                self.tb_process.stdin.flush()
            except BrokenPipeError:
                pass

    @property
    def bandwidth(self):
        return self._bandwidth

    @bandwidth.setter
    def bandwidth(self, value):
        self._bandwidth = value
        if self.tb_process:
            try:
                self.tb_process.stdin.write(b'BW:' + bytes(str(value), "utf8") + b'\n')
                self.tb_process.stdin.flush()
            except BrokenPipeError:
                pass

    def initalize_process(self):
        self.started.emit()

        if not hasattr(sys, 'frozen'):
            rp = os.path.dirname(os.path.realpath(__file__))
        else:
            rp = os.path.join(os.path.dirname(sys.executable), "dev", "gr")

        rp = os.path.realpath(os.path.join(rp, "scripts"))
        suffix = "_recv.py" if self._receiving else "_send.py"
        filename = self.device.lower() + suffix

        if not self.python2_interpreter:
            raise Exception("Could not find python 2 interpreter. Make sure you have a running gnuradio installation.")

        options = [self.python2_interpreter, os.path.join(rp, filename),
                   "--samplerate", str(self.sample_rate), "--freq", str(self.freq),
                   "--gain", str(self.gain), "--bandwidth", str(self.bandwidth),
                   "--port", str(self.port)]

        if self.device.upper() == "USRP":
            options.extend(["--ip", self.usrp_ip])

        logger.info("Starting Gnuradio")
        self.tb_process = Popen(options, stdout=PIPE, stderr=PIPE, stdin=PIPE, bufsize=1)
        logger.info("Started Gnuradio")
        t = Thread(target=self.enqueue_output, args=(self.tb_process.stderr, self.queue))
        t.daemon = True  # thread dies with the program
        t.start()

    def init_recv_socket(self):
        logger.info("Initalizing receive socket")
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PULL)
        logger.info("Initalized receive socket")

        while not self.isInterruptionRequested():
            try:
                time.sleep(0.1)
                logger.info("Trying to get a connection to gnuradio...")
                self.socket.connect("tcp://{0}:{1}".format(self.ip, self.port))
                logger.info("Got connection")
                break
            except (ConnectionRefusedError, ConnectionResetError):
                continue
            except Exception as e:
                logger.error("Unexpected error", str(e))

    def run(self):
        pass

    def read_errors(self):
        result = []
        while True:
            try:
                result.append(self.queue.get_nowait())
            except Empty:
                break

        result = b"".join(result)
        return result.decode("utf-8")

    def enqueue_output(self, out, queue):
        for line in iter(out.readline, b''):
            queue.put(line)
        out.close()

    def stop(self, msg: str):
        if msg and not msg.startswith("FIN"):
            self.requestInterruption()

        if self.tb_process:
            logger.info("Kill grc process")
            self.tb_process.kill()
            logger.info("Term grc process")
            self.tb_process.terminate()
            self.tb_process = None

        logger.info(msg)
        self.stopped.emit()
