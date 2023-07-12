# Copyright (C) 2023 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
# See: https://spdx.org/licenses/

from itertools import accumulate
import sys
try:
    import dv_processing as dv 
except ImportError:
    print("Need `dv_processing` library installed.", file=sys.stderr)
    exit(1)  

import numpy as np
import time
from enum import Enum
import typing as ty

from lava.magma.core.run_configs import Loihi2SimCfg
from lava.magma.core.run_conditions import RunSteps, RunContinuous
from lava.magma.core.decorator import implements, requires, tag
from lava.magma.core.model.py.model import PyLoihiProcessModel, PyAsyncProcessModel
from lava.magma.core.model.py.ports import PyOutPort, PyInPort
from lava.magma.core.model.py.type import LavaPyType
from lava.magma.core.process.ports.ports import OutPort, InPort
from lava.magma.core.process.process import AbstractProcess
from lava.magma.core.process.variable import Var
from lava.magma.core.resources import CPU
from lava.magma.core.sync.protocols.loihi_protocol import LoihiProtocol
import math  
import inspect

from lava.lib.peripherals.dvs.transform import Compose, EventVolume
from metavision_ml.preprocessing.event_to_tensor import histo_quantized
import warnings

class InivationCamera(AbstractProcess):
    """
    Process that receives events from Inivation device and sends them out as a histogram. 

    Parameters
    ----------
    device: str
        String to filename if reading from a RAW file or empty string for using a camera.
    biases: dict
        Dictionary of biases for the DVS Camera.
    # filters: list
    #     List containing inivation filters.
    # max_events_per_dt: int
    #     Maximum events that can be buffered in each timestep.
    transformations: Compose
        Tonic transformations to be applied to the events before sending them out.
    num_output_time_bins: int
        The number of output time bins to use for the ToFrame transformation.
    """

    def __init__(self,
                 sensor_shape: tuple,
                 device: str,
                 biases: dict = None,
                 # filters: list = [],
                 # max_events_per_dt: int = 10 ** 8,
                 transformations: Compose = None,
                 num_output_time_bins: int = 1,
                 out_shape: tuple = None,
                 ):

        # if not isinstance(max_events_per_dt, int) or max_events_per_dt < 0:
        #     raise ValueError("max_events_per_dt must be a positive integer value.")

        if not isinstance(num_output_time_bins, int) or num_output_time_bins < 0 or num_output_time_bins > 1:
            raise ValueError("Not Implemented: num_output_time_bins must be 1.")

        if not biases is None and not device == "":
            raise ValueError("Cant set biases if reading from file.")

        self.device = device
        self.biases = biases

        # self.max_events_per_dt = max_events_per_dt
        # self.filters = filters
        self.transformations = transformations
        self.num_output_time_bins = num_output_time_bins

        height, width = sensor_shape

        if out_shape is not None:
            self.shape = out_shape
        # Automatically determine out_shape
        else:
            event_shape = EventVolume(height=height, width=width, polarities=2)
            if transformations is not None:
                event_shape = self.transformations.determine_output_shape(event_shape)
            self.shape = (num_output_time_bins,
                          event_shape.polarities,
                          event_shape.height,
                          event_shape.width)
        # Check whether provided transformation is valid
        if self.transformations is not None:
            try:
                # Generate some artificial data
                n_random_spikes = 1000
                test_data = np.zeros(n_random_spikes, dtype=np.dtype([("y", int), ("x", int), ("p", int), ("t", int)]))
                test_data["x"] = np.random.rand(n_random_spikes) * width
                test_data["y"] = np.random.rand(n_random_spikes) * height
                test_data["p"] = np.random.rand(n_random_spikes) * 2
                test_data["t"] = np.sort(np.random.rand(n_random_spikes) * 1e6)

                # Transform data
                self.transformations(test_data)
                if len(test_data) > 0:
                    volume = np.zeros(self.shape, dtype=np.uint8)
                    histo_quantized(test_data, volume, np.max(test_data['t']))
            except Exception:
                raise Exception("Your transformation is not compatible with the provided data.")

        self.s_out = OutPort(shape=self.shape)

        super().__init__(shape=self.shape,
                         biases=self.biases,
                         device=self.device,
                         # filters=self.filters,
                         # max_events_per_dt=self.max_events_per_dt,
                         transformations=self.transformations,
                         num_output_time_bins=1)


@implements(proc=InivationCamera, protocol=LoihiProtocol)
@requires(CPU)
@tag('floating_pt')
class PyInivationCameraModel(PyLoihiProcessModel):
    s_out: PyOutPort = LavaPyType(PyOutPort.VEC_DENSE, np.int32)

    def __init__(self, proc_params):
        super().__init__(proc_params)
        self.shape = proc_params['shape']
        self.num_output_time_bins, self.polarities, self.height, self.width = self.shape
        self.device = proc_params['device']
        # self.filters = proc_params['filters']
        # self.max_events_per_dt = proc_params['max_events_per_dt']
        self.biases = proc_params['biases']
        self.transformations = proc_params['transformations']

        self.capture = dv.io.CameraCapture()

        if not self.biases is None:
            raise NotImplementedError("Biases are not implemented yet.")
            # Setting Biases for DVS camera
            device_biases = self.reader.device.get_i_ll_biases()
            for k, v in self.biases.items():
                device_biases.set(k, v)

        self.volume = np.zeros((self.num_output_time_bins, self.polarities, self.height, self.width), dtype=np.uint8)
        self.t_pause = time.time_ns()
        self.t_last_iteration = time.time_ns()

    def run_spk(self):
        """Load events from DVS, apply filters and transformations and send spikes as frame """

        # Time passed since last iteration
        t_now = time.time_ns()

        # Load new events since last iteration
        if self.t_pause > self.t_last_iteration:
            raise NotImplementedError("Pausing it not implemented yet.")
            # Runtime was paused in the meantime
            delta_t = np.max([10000, (self.t_pause - self.t_last_iteration) // 1000])
            delta_t_drop = np.max([10000, (t_now - self.t_pause) // 1000])

            events = self.reader.load_delta_t(delta_t)
            _ = self.reader.load_delta_t(delta_t_drop)
        else:
            # Runtime was not paused in the meantime
            delta_t = np.max([10000, (t_now - self.t_last_iteration) // 1000])
            #events = self.reader.load_delta_t(delta_t)
            events = self.capture.getNextEventBatch()

        if events is not None:
            events_np = events.numpy()
            events = np.zeros(len(events_np), dtype=np.dtype([("y", int), ("x", int), ("p", int), ("t", int)]))
            events['t'] = events_np['timestamp']
            events['p'] = events_np['polarity']
            events['x'] = events_np['x']
            events['y'] = events_np['y']
        else:
            events = np.array([])


        # Apply filters to events
        # for filter in self.filters:
        #     events_out = filter.get_empty_output_buffer()
        #     filter.process_events(events, events_out)
        #     events = events_out

        # if len(self.filters) > 0:
        #     events = events.numpy()

        # Transform events
        if not self.transformations is None and len(events) > 0:
            self.transformations(events)

        # Transform to frame
        if len(events) > 0:
            histo_quantized(events, self.volume, events['t'][-1] - events['t'][0], reset=True)
            frames = self.volume
        else:
            frames = np.zeros(self.s_out.shape)


        # Send
        self.s_out.send(frames)
        self.t_last_iteration = t_now

    def _pause(self):
        """Pause was called by the runtime"""
        super()._pause()
        self.t_pause = time.time_ns()
