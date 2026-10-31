"""Siglent SSA3000X / SSA3000X-R / SSA3000X Plus (tested: SSA3075X-R, fw 3.2.2.6)."""
from .base import steps_125
from .registry import register
from .scpi import ScpiSpectrumAnalyzer


@register
class SiglentSSA3000X(ScpiSpectrumAnalyzer):
    model_name = "Siglent SSA3000X"
    needs_host = True
    address_label = "IP address"
    default_address = "192.168.10.108"
    rbw_steps = steps_125(1, 1e6)
    min_freq = 9e3
    max_freq = 7.5e9

    # The base class command set is Siglent's dialect already.
    CMD = dict(ScpiSpectrumAnalyzer.CMD)

    STATE = [":SENS:FREQ:CENT", ":SENS:FREQ:SPAN", ":DISP:WIND:TRAC:Y:RLEV",
             ":SENS:POW:RF:ATT", ":SENS:POW:RF:ATT:AUTO",
             ":SENS:BWID:RES", ":SENS:BWID:RES:AUTO", ":SENS:BWID:VID", ":SENS:BWID:VID:AUTO",
             ":SENS:DET:TRAC1", ":TRAC1:MODE", ":SENS:AVER:TYPE",
             ":SENS:SWE:TIME", ":SENS:SWE:TIME:AUTO", ":INIT:CONT"]
    FCOUNT = ":CALC:MARK1:FCO"

    def save_state(self):
        state = super().save_state()
        state[self.FCOUNT] = self.query(self.FCOUNT + "?")
        return state

    def setup_noise_mode(self):
        # The marker frequency counter must be off while measuring: with it on, the
        # remote interface hangs (no reply to *OPC?, the instrument has to be rebooted).
        self.write(self.FCOUNT + " OFF")
        super().setup_noise_mode()

    def restore_state(self, state):
        super().restore_state(state)
        # Switching the counter on during a single sweep and then re-enabling continuous
        # mode hangs the instrument; once it is sweeping continuously it is safe.
        if state.get(self.FCOUNT, "").strip() in ("1", "ON"):
            self.write(self.FCOUNT + " ON")
            self.wait()
