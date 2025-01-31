# -*- coding: utf-8 -*-

"""
shepherd.__init__
~~~~~
Provides main API functionality for recording and emulation with shepherd.


:copyright: (c) 2019 Networked Embedded Systems Lab, TU Dresden.
:license: MIT, see LICENSE for more details.
"""

import logging
import time
import sys
from logging import NullHandler
from pathlib import Path
from contextlib import ExitStack
import invoke
import signal

from .datalog import LogReader
from .datalog import LogWriter
from .datalog import ExceptionRecord
from .eeprom import EEPROM
from .eeprom import CapeData
from .calibration import CalibrationData
from .shepherd_io import ShepherdIOException
from .shepherd_io import ShepherdIO
from . import commons

# Set default logging handler to avoid "No handler found" warnings.
logging.getLogger(__name__).addHandler(NullHandler())

logger = logging.getLogger(__name__)


class Recorder(ShepherdIO):
    """API for recording data with shepherd.

    Provides an easy to use, high-level interface for recording data with
    shepherd. Configures all hardware and initializes the communication
    with kernel module and PRUs.

    Args:
        mode (str): Should be either 'harvesting' to record harvesting data
            or 'load' to record target consumption data.
        load (str): Selects, which load should be used for recording.
            Should be one of 'artificial' or 'node'.
        harvesting_voltage (float): Fixed reference voltage for boost
            converter input.
        init_charge (bool): True to pre-charge the capacitor before starting
            recording.

    """

    def __init__(
        self,
        mode: str = "harvesting",
        load: str = "artificial",
        harvesting_voltage: float = None,
        init_charge: bool = False,
    ):
        super().__init__(mode, init_charge, load)

        self.harvesting_voltage = harvesting_voltage

    def __enter__(self):
        super().__enter__()

        if self.harvesting_voltage is not None:
            self.set_mppt(False)
            self.set_harvesting_voltage(self.harvesting_voltage)
        else:
            self.set_mppt(True)

        # In 'load' mode, the target is supplied from constant voltage reg
        if self.mode == "load":
            self.set_v_fixed(True)
            self.set_harvester(False)
        elif self.mode == "harvesting":
            self.set_harvester(True)

        # Give the PRU empty buffers to begin with
        for i in range(self.n_buffers):
            time.sleep(float(self.buffer_period_ns) / 1e9)
            self.release_buffer(i)
            logger.debug(f"sent empty buffer {i}")

        return self

    def release_buffer(self, index: int):
        """Returns a buffer to the PRU

        After reading the content of a buffer and potentially filling it with
        emulation data, we have to release the buffer to the PRU to avoid it
        running out of buffers.

        Args:
            index (int): Index of the buffer. 0 <= index < n_buffers
        """
        self._release_buffer(index)


class Emulator(ShepherdIO):
    """API for emulating data with shepherd.

    Provides an easy to use, high-level interface for emulating data with
    shepherd. Configures all hardware and initializes the communication
    with kernel module and PRUs.

    Args:
        calibration_recording (CalibrationData): Shepherd calibration data
            belonging to the IV data that is being emulated
        calibration_emulation (CalibrationData): Shepherd calibration data
            belonging to the cape used for emulation
        load (str): Selects, which load should be used for recording.
            Should be one of 'artificial' or 'node'.
        init_charge (bool): True to pre-charge the capacitor before starting
            recording.
    """

    def __init__(
        self,
        initial_buffers: list,
        calibration_recording: CalibrationData = None,
        calibration_emulation: CalibrationData = None,
        load: str = "node",
        init_charge: bool = False,
    ):
        super().__init__("emulation", init_charge, load)

        if calibration_emulation is None:
            calibration_emulation = CalibrationData.from_default()
            logger.warning(
                "No emulation calibration data provided - using defaults"
            )
        if calibration_recording is None:
            calibration_recording = CalibrationData.from_default()
            logger.warning(
                "No recording calibration data provided - using defaults"
            )
        # Values from recording are binary ADC values. We have to send binary
        # DAC values to the DAC for emulation. To directly convert ADC to DAC
        # values, we precalculate the 'transformation coefficients' based on
        # calibration data from the recorder and the emulator
        self.transform_coeffs = {"voltage": dict(), "current": dict()}
        for channel in ["voltage", "current"]:
            self.transform_coeffs[channel]["gain"] = (
                calibration_recording["harvesting"][channel]["gain"]
                * calibration_emulation["emulation"][channel]["gain"]
            )
            self.transform_coeffs[channel]["offset"] = (
                calibration_emulation["emulation"][channel]["gain"]
                * calibration_recording["harvesting"][channel]["offset"]
                + calibration_emulation["emulation"][channel]["offset"]
            )

        self._initial_buffers = initial_buffers

    def __enter__(self):
        super().__enter__()

        # Disconnect harvester to avoid leakage in or out of the harvester
        self.set_harvester(False)
        # We will dynamically generate the reference voltage for the boost
        # converter. This only takes effect if MPPT is disabled.
        self.set_mppt(False)

        # Preload emulator with some data
        for idx, buffer in enumerate(self._initial_buffers):
            time.sleep(float(self.buffer_period_ns) / 1e9)
            self.put_buffer(idx, buffer)

        return self

    def put_buffer(self, index, buffer):

        ts_start = time.time()

        # Convert binary ADC recordings to binary DAC values
        voltage_transformed = (
            buffer.voltage * self.transform_coeffs["voltage"]["gain"]
            + self.transform_coeffs["voltage"]["offset"]
        ).astype("u4")

        current_transformed = (
            buffer.current * self.transform_coeffs["current"]["gain"]
            + self.transform_coeffs["current"]["offset"]
        ).astype("u4")

        self.shared_mem.write_buffer(
            index, voltage_transformed, current_transformed
        )
        self._release_buffer(index)

        logger.debug(
            (
                f"Returning buffer #{ index } to PRU took "
                f"{ time.time()-ts_start }"
            )
        )


class ShepherdDebug(ShepherdIO):
    """API for direct access to ADC and DAC.

    For debugging purposes, running the GUI or for retrieving calibration
    values, we need to directly read values from the ADC and set voltage using
    the DAC. This class allows to put the underlying PRUs and kernel module in
    a mode, where they accept 'debug messages' that allow to directly interface
    with the ADC and DAC.
    """

    def __init__(self):
        super().__init__("debug", False, "artificial")

    def adc_read(self, channel: str):
        """Reads value from specified ADC channel.

        Args:
            channel (str): Specifies the channel to read from, e.g., 'v_in' for
                harvesting voltage or 'i_out' for load current
        Returns:
            Binary ADC value read from corresponding channel
        """
        if channel.lower() == "v_in":
            channel_no = 0
        elif channel.lower() == "v_out":
            channel_no = 1
        elif channel.lower() in ["a_in", "i_in"]:
            channel_no = 2
        elif channel.lower() in ["a_out", "i_out"]:
            channel_no = 3
        else:
            raise ValueError(f"ADC channel { channel } unknown")

        self._send_msg(commons.MSG_DEP_DBG_ADC, channel_no)

        msg_type, value = self._get_msg()
        if msg_type != commons.MSG_DEP_DBG_ADC:
            raise ShepherdIOException(
                (
                    f"Expected msg type { commons.MSG_DEP_DBG_ADC } "
                    f"got { msg_type }[{ value }]"
                )
            )

        return value

    def dac_write(self, channel: str, value: int):
        """Writes value to specified DAC channel

        Args:
            channel (str): Specifies the channel to write to, e.g., 'current'
                for current channel or 'v' for voltage channel
            value (int): Binary DAC value to be sent to corresponding channel
        """
        # For a mapping of DAC channel to command refer to TI DAC8562T
        # datasheet Table 17
        if channel.lower() in ["current", "i", "a"]:
            dac_command = value
        elif channel.lower() in ["voltage", "v"]:
            # The DAC 'voltage' channel is on channel B
            dac_command = value | (1 << 16)
        else:
            raise ValueError(f"DAC channel { channel } unknown")

        self._send_msg(commons.MSG_DEP_DBG_DAC, dac_command)

    def get_buffer(self, timeout=None):
        raise NotImplementedError("Method not implemented for debugging mode")


def record(
    output: Path,
    mode: str = "harvesting",
    length: float = None,
    force: bool = False,
    no_calib: bool = False,
    harvesting_voltage: float = None,
    load: str = "artificial",
    init_charge: bool = False,
    start_time: float = None,
):
    """Starts recording.

    Args:
        output (Path): Path of hdf5 file where IV measurements should be
            stored
        mode (str): 'harvesting' for recording harvesting data, 'load' for
            recording load consumption data.
        length (float): Maximum time duration of emulation in seconds
        force (bool): True to overwrite existing file under output path,
            False to store under different name
        no_calib (bool): True to use default calibration values, False to
            read calibration data from EEPROM
        harvesting_voltage (float): Sets a fixed reference voltage for the
            input of the boost converter. Alternative to MPPT algorithm.
        load (str): Type of load. 'artificial' for dummy, 'node' for sensor
            node
        init_charge (bool): True to pre-charge capacitor before starting
            emulation
        start_time (float): Desired start time of emulation in unix epoch time
    """
    if no_calib:
        calib = CalibrationData.from_default()
    else:
        with EEPROM() as eeprom:
            try:
                calib = eeprom.read_calibration()
            except ValueError:
                logger.warning(
                    "Couldn't read calibration from EEPROM. Falling back to default values."
                )
                calib = CalibrationData.from_default()

    recorder = Recorder(
        mode=mode,
        load=load,
        harvesting_voltage=harvesting_voltage,
        init_charge=init_charge,
    )
    log_writer = LogWriter(
        store_path=output, calibration_data=calib, mode=mode, force=force
    )
    with ExitStack() as stack:

        stack.enter_context(recorder)
        stack.enter_context(log_writer)

        res = invoke.run("hostname", hide=True, warn=True)
        log_writer["hostname"] = res.stdout

        recorder.start(start_time, wait_blocking=False)
        if start_time is None:
            recorder.wait_for_start(15)
        else:
            logger.info(f"waiting {start_time - time.time():.2f}s until start")
            recorder.wait_for_start(start_time - time.time() + 15)

        logger.info("shepherd started!")

        def exit_gracefully(signum, frame):
            stack.close()
            sys.exit(0)

        signal.signal(signal.SIGTERM, exit_gracefully)
        signal.signal(signal.SIGINT, exit_gracefully)

        if length is None:
            ts_end = sys.float_info.max
        else:
            ts_end = time.time() + length

        while time.time() < ts_end:
            try:
                idx, buf = recorder.get_buffer()
            except ShepherdIOException as e:
                logger.error(
                    f"ShepherdIOException(ID={e.id}, val={e.value}): {str(e)}"
                )
                err_rec = ExceptionRecord(
                    int(time.time() * 1e9), str(e), e.value
                )
                log_writer.write_exception(err_rec)

                raise

            log_writer.write_buffer(buf)
            recorder.release_buffer(idx)


def emulate(
    input: Path,
    output: Path = None,
    length: float = None,
    force: bool = False,
    no_calib: bool = False,
    load: str = "artificial",
    init_charge: bool = False,
    start_time: float = None,
):
    """Starts emulation.

    Args:
        input (Path): path of hdf5 file containing recorded
            harvesting data
        output (Path): Path of hdf5 file where load measurements should
            be stored
        length (float): Maximum time duration of emulation in seconds
        force (bool): True to overwrite existing file under output,
            False to store under different name
        no_calib (bool): True to use default calibration values, False to
            read calibration data from EEPROM
        load (str): Type of load. 'artificial' for dummy, 'node' for sensor
            node
        init_charge (bool): True to pre-charge capacitor before starting
            emulation
        start_time (float): Desired start time of emulation in unix epoch time
    """

    if no_calib:
        calib = CalibrationData.from_default()
    else:
        with EEPROM() as eeprom:
            try:
                calib = eeprom.read_calibration()
            except ValueError:
                logger.warning(
                    "Couldn't read calibration from EEPROM. Falling back to default values."
                )
                calib = CalibrationData.from_default()

    if output is not None:
        log_writer = LogWriter(
            store_path=output, force=force, mode="load", calibration_data=calib
        )

    log_reader = LogReader(input, 10_000)

    with ExitStack() as stack:
        if output is not None:
            stack.enter_context(log_writer)

        stack.enter_context(log_reader)

        emu = Emulator(
            calibration_recording=log_reader.get_calibration_data(),
            calibration_emulation=calib,
            initial_buffers=log_reader.read_buffers(end=64),
            init_charge=init_charge,
            load=load,
        )
        stack.enter_context(emu)

        emu.start(start_time, wait_blocking=False)
        if start_time is None:
            emu.wait_for_start(15)
        else:
            logger.info(f"waiting {start_time - time.time():.2f}s until start")
            emu.wait_for_start(start_time - time.time() + 15)

        logger.info("shepherd started!")

        def exit_gracefully(signum, frame):
            stack.close()
            sys.exit(0)

        signal.signal(signal.SIGTERM, exit_gracefully)
        signal.signal(signal.SIGINT, exit_gracefully)

        if length is None:
            ts_end = sys.float_info.max
        else:
            ts_end = time.time() + length

        for hrvst_buf in log_reader.read_buffers(start=64):
            try:
                idx, load_buf = emu.get_buffer(timeout=1)
            except ShepherdIOException as e:
                logger.error(
                    f"ShepherdIOException(ID={e.id}, val={e.value}): {str(e)}"
                )
                if output is not None:
                    err_rec = ExceptionRecord(
                        int(time.time() * 1e9), str(e), e.value
                    )
                    log_writer.write_exception(err_rec)

                raise

            if output is not None:
                log_writer.write_buffer(load_buf)

            emu.put_buffer(idx, hrvst_buf)

            if time.time() > ts_end:
                break

        # Read all remaining buffers from PRU
        while True:
            try:
                idx, load_buf = emu.get_buffer(timeout=1)
                if output is not None:
                    log_writer.write_buffer(load_buf)
            except ShepherdIOException as e:
                break
