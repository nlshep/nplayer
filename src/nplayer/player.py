"""Main functionality of the music player."""

import time
import logging
import os
import threading

import RPIO
import gi
from gi.repository import GObject, Gst
gi.require_version('Gst', '1.0')

class NativityPlayer(object):
    """Implementation class of the music player."""

    def __init__(self, cfg):
        """Initializes the player. cfg is a ConfigParser.ConfigParser instance
        containing the player configuration."""

        self.log = logging.getLogger('nplayer')

        #load up confguration
        self.pin_play = cfg.getint('inputs', 'pin_play')
        self.pin_stop = cfg.getint('inputs', 'pin_stop')
        self.pin_rw = cfg.getint('inputs', 'pin_rw')
        self.pin_ff = cfg.getint('inputs', 'pin_ff')
        self.pin_scene = cfg.getint('inputs', 'pin_scene')
        self.pin_sctoggle = cfg.getint('inputs', 'pin_scene_toggle')
        self._pins = (self.pin_play, self.pin_stop, self.pin_rw, self.pin_ff,
            self.pin_scene, self.pin_sctoggle)

        self.db_time = cfg.getint('inputs', 'db_time')
        self.libdir = cfg.get('fs', 'libdir')

        self.skip_len = cfg.getint('prefs', 'skip_len')

        #flags for whether each input is high (True) or low (False), keyed by
        #pin number
        self._in_states = {
            self.pin_play: False,
            self.pin_stop: False,
            self.pin_rw: False,
            self.pin_ff: False,
            self.pin_scene: False,
            self.pin_sctoggle: False
        }

        #map for pin input handlers based on pin and state
        self._handler_map = {
            self.pin_play: { True: self._h_play_r, False: self._h_play_f },
            self.pin_stop: { True: self._h_stop_r, False: self._h_stop_f },
            self.pin_rw: { True: self._h_rw_r, False: self._h_rw_f },
            self.pin_ff: { True: self._h_ff_r, False: self._h_ff_f },
            self.pin_scene: { True: self._h_scene_r, False: self._h_scene_f },
            self.pin_sctoggle: {
                True: self._h_sctoggle_r, False: self._h_sctoggle_f
            }
        }

        #flag used for MP3 switching to ignore a play button release if we've
        #just switched MP3s (meaning the play button was pressed down as part
        #of the switch action, not because the user wants to start playing)
        self._ign_play = False

        #pre-load list of files
        self.files =\
            [os.path.join(self.libdir, x) for x in os.listdir(self.libdir)]
        if not self.files:
            raise Exception('no files in library dir %s' % self.libdir)
        else:
            self.files.sort()

        if cfg.has_option('fs', 'def_file'):
            def_file = os.path.join(self.libdir, cfg.get('fs', 'def_file'))
            if os.path.isfile(def_file):
                self.cur_file = def_file
                self.cur_fileno = self.files.index(def_file)
            else:
                self.cur_file = self.files[0]
                self.cur_fileno = 0
        else:
            self.cur_file = self.files[0]
            self.cur_fileno = 0

        self.log.info('chose default file %s (index %d)', self.cur_file,
            self.cur_fileno)

        #set up GStreamer
        GObject.threads_init()
        Gst.init(None)
        self.log.info('gstreamer initialized')

        #set up file player
        self.player = Gst.ElementFactory.make('playbin', 'player')
        self.player.set_property('uri', 'file://%s'%self.cur_file)
        self.pl_bus = self.player.get_bus()
        self.log.info('player initialized')

        #event to provoke an LCD update
        self._upd_evt = threading.Event()

        #timer to provoke LCD updates for playing progress
        self._upd_timer = None


    def start(self):
        """Starts accepting input, then blocks forever."""
        #set up pins
        for pin in self._pins:
            RPIO.add_interrupt_callback(pin, self._input_cb,
                edge='both', pull_up_down=RPIO.PUD_DOWN,
                debounce_timeout_ms=self.db_time)

        RPIO.wait_for_interrupts(threaded=True)

        #main LCD update loop
        while self._upd_evt.wait():

            self._upd_evt.clear()

            msg = 'file %s' % self.cur_file

            if self.player.current_state == Gst.State.PLAYING:
                msg += ' (playing, %f %%)' %\
                    (self.player.query_position(Gst.Format.PERCENT)[1]/10000.0,)

                #drain out messages from the player bus to see if the stream is
                #done
                gmsg = self.pl_bus.pop()
                while gmsg is not None:
                    if gmsg.type == Gst.MessageType.EOS:
                        self.log.debug('got end of stream, resetting')
                        self.player.set_state(Gst.State.READY)
                        break
                    else:
                        gmsg = self.pl_bus.pop()
                else:
                    #still playing, so set the timer to do another update
                    self._upd_timer = threading.Timer(0.5, self._trigger_update)
                    self._upd_timer.start()

            print msg


    def _trigger_update(self):
        """Triggers an LCD update."""
        self._upd_evt.set()


    def _input_cb(self, pin, istate):
        """Callback for GPIO event detection.
        
        Context: callback thread"""

        if istate: #button pressed
            self._in_states[pin] = True
        else: #button released
            self._in_states[pin] = False

        self._handler_map[pin][istate]()


    def _h_play_r(self):
        """Play pressed; nothing to do here."""
        pass


    def _h_play_f(self):
        """Play button released"""

        if True in (self._in_states[self.pin_rw], self._in_states[self.pin_ff]):
            #either rw or ff are pressed down, so this was a botched attempt
            #(on the user's part) to switch MP3 file
            pass
        elif self._ign_play:
            #play is masked due to an MP3 switch
            self._ign_play = False
            return
        elif self.player.current_state != Gst.State.PLAYING:
            #a pure play button release, and we're not yet playing, so start
            self.player.set_state(Gst.State.PLAYING)
            self._wait_playing()
            self._upd_evt.set()
        #else: already playing, ignore


    def _h_stop_r(self):
        pass


    def _h_stop_f(self):
        """Stop button released, stop playing if currently playing."""
        if self.player.current_state == Gst.State.PLAYING:
            self.player.set_state(Gst.State.READY)
            self._upd_evt.set()


    def _h_rw_r(self):
        """Rewind button pressed; user may either be trying to rewind or
        cycle the MP3 to be played."""
        if self._in_states[self.pin_play]:
            #play is currently pressed, so this will be a request to change the
            #MP3 (on release), so we do nothing yet
            pass
        else:
            #play not pressed, so this is the start of a rewind command
            #TODO
            pass


    def _h_rw_f(self):
        """Rewind button released."""
        if self._in_states[self.pin_play]:
            #play is pressed, so this is an MP3 change
            if self.player.current_state == Gst.State.PLAYING:
                self.player.set_state(Gst.State.READY)

            self.cur_fileno = (self.cur_fileno - 1) % len(self.files)
            self.cur_file = self.files[self.cur_fileno]
            self.player.set_property('uri', 'file://%s'%self.cur_file)
            self._upd_evt.set()
            self._ign_play = True
        else:
            #a rewind request
            if self.player.current_state == Gst.State.PLAYING:
                cur_pos = self.player.query_position(Gst.Format.TIME)[1]
                new_pos = max(0, cur_pos - self.skip_len*10**9)
                self.player.seek(1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH,
                    Gst.SeekType.SET, new_pos, Gst.SeekType.NONE, -1)
                self._wait_playing()
                self._upd_evt.set()


    def _h_ff_r(self):
        """Fast-forward button pressed; may be start of either fast-forward or
        MP3 cycle."""
        if self._in_states[self.pin_play]:
            #play is pressed, so MP3 cycle is starting
            pass
        else:
            #play not pressed, so this is the start of a fast-forward
            #TODO
            pass


    def _h_ff_f(self):
        """Fast-forward button released."""
        if self._in_states[self.pin_play]:
            #play is pressed, so this is an MP3 change
            if self.player.current_state == Gst.State.PLAYING:
                self.player.set_state(Gst.State.READY)

            self.cur_fileno = (self.cur_fileno + 1) % len(self.files)
            self.cur_file = self.files[self.cur_fileno]
            self.player.set_property('uri', 'file://%s'%self.cur_file)
            self._upd_evt.set()
            self._ign_play = True
        else:
            #a fast-forward request
            if self.player.current_state == Gst.State.PLAYING:
                cur_pos = self.player.query_position(Gst.Format.TIME)[1]
                new_pos = max(0, cur_pos + self.skip_len*10**9)
                self.player.seek(1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH,
                    Gst.SeekType.SET, new_pos, Gst.SeekType.NONE, -1)
                self._wait_playing()
                self._upd_evt.set()


    def _h_scene_r(self):
        pass
    def _h_scene_f(self):
        pass
    def _h_sctoggle_r(self):
        pass
    def _h_sctoggle_f(self):
        pass


    def _wait_playing(self):
        """Blocks waiting for the player to start playing."""
        self.player.get_state(timeout=Gst.CLOCK_TIME_NONE)
