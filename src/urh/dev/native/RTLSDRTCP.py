from multiprocessing import Process
from multiprocessing import Value

import numpy as np
from multiprocessing import Pipe

from urh.dev.native.Device import Device
from urh.util.Logger import logger

import socket

def receive_sync(connection, device_number: int, center_freq: int, sample_rate: int, gain: int):
    rtlsdrtcp.open("127.0.0.1", 1234)
    rtlsdrtcp.set_parameter("centerFreq", center_freq)
    rtlsdrtcp.set_parameter("sampleRate", sample_rate)
    rtlsdrtcp.set_parameter("tunerGain", gain)
    #rtlsdrtcp.reset_buffer()
    exit_requested = False

    while not exit_requested:
        while connection.poll():
            result = process_command(connection.recv())
            if result == "stop":
                exit_requested = True
                break

        if not exit_requested:
            connection.send_bytes(rtlsdrtcp.read_sync())

    logger.debug("RTLSDRTCP: closing device")
    rtlsdrtcp.close()
    connection.close()
    pass

def process_command(command):
    logger.debug("RTLSDRTCP: {}".format(command))
    if command == "stop":
        return "stop"

    tag, value = command.split(":")
    if tag == "center_freq":
        logger.info("RTLSDR: Set center freq to {0}".format(int(value)))
        return rtlsdrtcp.set_parameter("centerFreq", int(value))

    elif tag == "tuner_gain":
        logger.info("RTLSDR: Set tuner gain to {0}".format(int(value)))
        return rtlsdrtcp.set_parameter("tunerGain", int(value))

    elif tag == "sample_rate":
        logger.info("RTLSDR: Set sample_rate to {0}".format(int(value)))
        return rtlsdrtcp.set_parameter("sampleRate", int(value))

    # elif tag == "tuner_bandwidth":
    #     logger.info("RTLSDR: Set bandwidth to {0}".format(int(value)))
    #     return rtlsdrtcp.set_parameter("bandwidth", int(value))


class RTLSDRTCP(Device):
    BYTES_PER_SAMPLE = 2  # RTLSDR device produces 8 bit unsigned IQ data
    MAXDATASIZE = 65536
    ENDIAN = "big"
    RTL_TCP_CONSTS = ["NULL", "centerFreq", "sampleRate", "tunerGainMode", "tunerGain", "freqCorrection", "tunerIFGain",
                      "testMode", "agcMode", "directSampling", "offsetTuning", "rtlXtalFreq", "tunerXtalFreq", "gainByIndex"]

    def __init__(self, freq, gain, srate, device_number, is_ringbuffer=False):
        super().__init__(0, freq, gain, srate, is_ringbuffer)

        self.open() #open("127.0.0.1", 1234)

        self.success = 0

        self.is_receiving_p = Value('i', 0)
        """
        Shared Value to communicate with the receiving process.

        """

        #self.bandwidth_is_adjustable = hasattr(rtlsdr, "set_tuner_bandwidth")   # e.g. not in Manjaro Linux / Ubuntu 14.04
        self._max_frequency = 6e9
        self._max_sample_rate = 3200000
        self._max_frequency = 6e9
        self._max_bandwidth = 3200000
        self._max_gain = 500  # Todo: Consider get_tuner_gains for allowed gains here

        self.device_number = device_number

    def open(self, hostname="127.0.0.1", port=1234):
        try:
            # Create socket and connect
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            self.sock.settimeout(1.0)   # Timeout 1s
            self.sock.connect((hostname, port))

            # Receive rtl_tcp initial data
            init_data = self.sock.recv(self.MAXDATASIZE)

            if len(init_data) != 12:
                return False
            if init_data[0:4] != b'RTL0':
                return False

            # Extract tuner name
            tuner_number = int.from_bytes(init_data[4:8], self.ENDIAN)
            if tuner_number == 1:
                self.tuner = "E4000"
            elif tuner_number == 2:
                self.tuner = "FC0012"
            elif tuner_number == 3:
                self.tuner = "FC0013"
            elif tuner_number == 4:
                self.tuner = "FC2580"
            elif tuner_number == 5:
                self.tuner = "R820T"
            elif tuner_number == 6:
                self.tuner = "R828D"
            else:
                self.tuner = "Unknown"

            # Extract IF and RF gain
            self.if_gain = int.from_bytes(init_data[8:10], self.ENDIAN)
            self.rf_gain = int.from_bytes(init_data[10:12], self.ENDIAN)

        except OSError as e:
            logger.info("Could not connect to rtl_tcp", hostname, port, "(", str(e), ")")

    def close(self):
        self.sock.close()

    def set_parameter(self, param, value):
        msg = self.RTL_TCP_CONSTS.index(param).to_bytes(1, self.ENDIAN)     # Set param at bits 0-7
        msg += value.to_bytes(4, self.ENDIAN)   # Set value at bits 8-39

        self.sock.sendall(msg)  # Send data to rtl_tcp
        ack = self.sock.recv(self.MAXDATASIZE)   # Await ACK
        if len(ack) == 0:
            return True
        else:
            return False

    # def get_parameter(self, param):
    #     msg = self.RTL_TCP_CONSTS.index(param).to_bytes(1, self.ENDIAN)  # Set param at bits 0-7
    #     value = 0
    #     msg += value.to_bytes(4, self.ENDIAN)  # Set value to zero
    #
    #     self.sock.sendall(msg)  # Send data to rtl_tcp
    #     info = self.sock.recv(self.MAXDATASIZE)  # Await ACK
    #     if len(info) > 0:
    #         return info
    #     else:
    #         return False

    def start_rx_mode(self):
        self.init_recv_buffer()

        self.is_open = True
        self.is_receiving = True
        self.receive_process = Process(target=receive_sync, args=(self.child_conn, self.device_number,
                                                                  self.frequency, self.sample_rate, self.gain
                                                                  ))
        self.receive_process.daemon = True
        self._start_read_rcv_buffer_thread()
        self.receive_process.start()

    def stop_rx_mode(self, msg):
        self.is_receiving = False
        self.parent_conn.send("stop")

        logger.info("RTLSDRTCP: Stopping RX Mode: " + msg)

        if hasattr(self, "receive_process"):
            self.receive_process.join(0.3)
            if self.receive_process.is_alive():
                logger.warning("RTLSDRTCP: Receive process is still alive, terminating it")
                self.receive_process.terminate()
                self.receive_process.join()
                self.parent_conn, self.child_conn = Pipe()

        if hasattr(self, "read_queue_thread") and self.read_recv_buffer_thread.is_alive():
            try:
                self.read_recv_buffer_thread.join(0.001)
                logger.info("RTLSDRTCP: Joined read_queue_thread")
            except RuntimeError:
                logger.error("RTLSDRTCP: Could not join read_queue_thread")


    def set_device_frequency(self, frequency):
        #self.parent_conn.send("center_freq:{}".format(int(frequency)))
        self.set_parameter("centerFreq", int(frequency))

    def set_device_sample_rate(self, sample_rate):
        #self.parent_conn.send("sample_rate:{}".format(int(sample_rate)))
        self.set_parameter("sampleRate", int(sample_rate))

    def set_freq_correction(self, ppm):
        ret = self.set_parameter("freqCorrection", int(ppm))    # True/False
        self.log_retcode(ret, "Set frequency correction")

    def set_offset_tuning(self, on: bool):
        ret = self.set_parameter("offsetTuning", on)
        self.log_retcode(ret, "Set offset tuning")

    def set_gain_mode(self, manual: bool):
        ret = self.set_parameter("tunerGainMode", manual)
        self.log_retcode(ret, "Set gain mode manual")

    def set_if_gain(self, gain):
        ret = self.set_parameter("tunerIFGain", int(gain))
        self.log_retcode(ret, "Set IF gain")

    def set_gain(self, gain):
        #self.parent_conn.send("tuner_gain:{}".format(int(gain)))
        self.set_parameter("tunerGain", int(gain))

    # def set_device_gain(self, gain):
    #     self.set_gain(gain)

    # def set_device_bandwidth(self, bandwidth):
    #     if hasattr(rtlsdr, "set_tuner_bandwidth"):
    #         #self.parent_conn.send("tuner_bandwidth:{}".format(int(bandwidth)))
    #         #self.set_parameter("bandwidth", int(bandwidth))    # Currently not supported
    #         pass
    #     else:
    #         logger.warning("Setting the bandwidth is not supported by your RTL-SDR driver version.")
    #     pass

    @staticmethod
    def unpack_complex(buffer, nvalues: int):
        """
        The raw, captured IQ data is 8 bit unsigned data.

        :return:
        """
        result = np.empty(nvalues, dtype=np.complex64)
        unpacked = np.frombuffer(buffer, dtype=[('r', np.uint8), ('i', np.uint8)])
        result.real = (unpacked['r'] / 127.5) - 1.0
        result.imag = (unpacked['i'] / 127.5) - 1.0
        return result

    @staticmethod
    def pack_complex(complex_samples: np.ndarray):
        return (127.5 * (complex_samples.view(np.float32) + 1.0)).astype(np.uint8).tostring()


# Test
rtlsdrtcp = RTLSDRTCP(926000000, 300, 2000000, 1)