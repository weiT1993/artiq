from migen import *

from artiq.gateware.rtio import cri
from artiq.gateware.rtio.sed import layouts


__all__ = ["LaneDistributor"]


# CRI write happens in 3 cycles:
# 1. set timestamp and channel
# 2. set other payload elements and issue write command
# 3. check status

class LaneDistributor(Module):
    def __init__(self, lane_count, seqn_width, layout_payload,
                 compensation, glbl_fine_ts_width,
                 enable_spread=True, quash_channels=[], interface=None):
        if lane_count & (lane_count - 1):
            raise NotImplementedError("lane count must be a power of 2")

        if interface is None:
            interface = cri.Interface()
        self.cri = interface
        self.sequence_error = Signal()
        self.sequence_error_channel = Signal(16)
        self.minimum_coarse_timestamp = Signal(64-glbl_fine_ts_width)
        self.output = [Record(layouts.fifo_ingress(seqn_width, layout_payload))
                       for _ in range(lane_count)]

        # # #

        o_status_wait = Signal()
        o_status_underflow = Signal()
        self.comb += self.cri.o_status.eq(Cat(o_status_wait, o_status_underflow))

        # internal state
        current_lane = Signal(max=lane_count)
        last_coarse_timestamp = Signal(64-glbl_fine_ts_width)
        last_lane_coarse_timestamps = Array(Signal(64-glbl_fine_ts_width)
                                            for _ in range(lane_count))
        seqn = Signal(seqn_width)

        # distribute data to lanes
        for lio in self.output:
            self.comb += [
                lio.seqn.eq(seqn),
                lio.payload.channel.eq(self.cri.chan_sel[:16]),
                lio.payload.timestamp.eq(self.cri.timestamp),
            ]
            if hasattr(lio.payload, "address"):
                self.comb += lio.payload.address.eq(self.cri.o_address)
            if hasattr(lio.payload, "data"):
                self.comb += lio.payload.data.eq(self.cri.o_data)

        # when timestamp and channel arrive in cycle #1, prepare computations
        us_timestamp_width = 64 - glbl_fine_ts_width
        coarse_timestamp = Signal(us_timestamp_width)
        self.comb += coarse_timestamp.eq(self.cri.timestamp[glbl_fine_ts_width:])
        min_minus_timestamp = Signal((us_timestamp_width + 1, True))
        laneAmin_minus_timestamp = Signal((us_timestamp_width + 1, True))
        laneBmin_minus_timestamp = Signal((us_timestamp_width + 1, True))
        last_minus_timestamp = Signal((us_timestamp_width + 1, True))
        current_lane_plus_one = Signal(max=lane_count)
        self.comb += current_lane_plus_one.eq(current_lane + 1)
        self.sync += [
            min_minus_timestamp.eq(self.minimum_coarse_timestamp - coarse_timestamp),
            laneAmin_minus_timestamp.eq(last_lane_coarse_timestamps[current_lane] - coarse_timestamp),
            laneBmin_minus_timestamp.eq(last_lane_coarse_timestamps[current_lane_plus_one] - coarse_timestamp),
            last_minus_timestamp.eq(last_coarse_timestamp - coarse_timestamp)
        ]

        quash = Signal()
        self.sync += quash.eq(0)
        for channel in quash_channels:
            self.sync += If(self.cri.chan_sel[:16] == channel, quash.eq(1))

        latency_compensation = Memory(14, len(compensation), init=compensation)
        latency_compensation_port = latency_compensation.get_port()
        self.specials += latency_compensation, latency_compensation_port 
        self.comb += latency_compensation_port.adr.eq(self.cri.chan_sel[:16]) 

        # cycle #2, write
        compensation = Signal((14, True))
        self.comb += compensation.eq(latency_compensation_port.dat_r)
        timestamp_above_min = Signal()
        timestamp_above_laneA_min = Signal()
        timestamp_above_laneB_min = Signal()
        timestamp_above_lane_min = Signal()
        force_laneB = Signal()
        use_laneB = Signal()
        use_lanen = Signal(max=lane_count)

        do_write = Signal()
        do_underflow = Signal()
        do_sequence_error = Signal()
        self.comb += [
            timestamp_above_min.eq(min_minus_timestamp - compensation < 0),
            timestamp_above_laneA_min.eq(laneAmin_minus_timestamp - compensation < 0),
            timestamp_above_laneB_min.eq(laneBmin_minus_timestamp - compensation < 0),
            If(force_laneB | (last_minus_timestamp - compensation >= 0),
                use_lanen.eq(current_lane + 1),
                use_laneB.eq(1)
            ).Else(
                use_lanen.eq(current_lane),
                use_laneB.eq(0)
            ),

            timestamp_above_lane_min.eq(Mux(use_laneB, timestamp_above_laneB_min, timestamp_above_laneA_min)),
            If(~quash,
                do_write.eq((self.cri.cmd == cri.commands["write"]) & timestamp_above_min & timestamp_above_lane_min),
                do_underflow.eq((self.cri.cmd == cri.commands["write"]) & ~timestamp_above_min),
                do_sequence_error.eq((self.cri.cmd == cri.commands["write"]) & timestamp_above_min & ~timestamp_above_lane_min),
            ),
            Array(lio.we for lio in self.output)[use_lanen].eq(do_write)
        ]
        compensated_timestamp = Signal(64)
        self.comb += compensated_timestamp.eq(self.cri.timestamp + (compensation << glbl_fine_ts_width))
        self.sync += [
            If(do_write,
                If(use_laneB, current_lane.eq(current_lane + 1)),
                last_coarse_timestamp.eq(compensated_timestamp[glbl_fine_ts_width:]),
                last_lane_coarse_timestamps[use_lanen].eq(compensated_timestamp[glbl_fine_ts_width:]),
                seqn.eq(seqn + 1),
            )
        ]
        for lio in self.output:
            self.comb += lio.payload.timestamp.eq(compensated_timestamp)

        # cycle #3, read status
        current_lane_writable = Signal()
        self.comb += [
            current_lane_writable.eq(Array(lio.writable for lio in self.output)[current_lane]),
            o_status_wait.eq(~current_lane_writable)
        ]
        self.sync += [
            If(self.cri.cmd == cri.commands["write"],
                o_status_underflow.eq(0)
            ),
            If(do_underflow,
                o_status_underflow.eq(1)
            ),
            self.sequence_error.eq(do_sequence_error),
            self.sequence_error_channel.eq(self.cri.chan_sel[:16])
        ]

        # current lane has been full, spread events by switching to the next.
        if enable_spread:
            current_lane_writable_r = Signal(reset=1)
            self.sync += [
                current_lane_writable_r.eq(current_lane_writable),
                If(~current_lane_writable_r & current_lane_writable,
                    force_laneB.eq(1)
                ),
                If(do_write,
                    force_laneB.eq(0)
                )
            ]
