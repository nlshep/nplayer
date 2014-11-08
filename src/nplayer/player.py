"""Main functionality of the music player."""

import time
import logging
import os
import threading

import RPIO
import gi
from gi.repository import GObject, Gst
gi.require_version('Gst', '1.0')

from nplayer import nhd_lcd

#error handling:
#-errors trying to cancel a timer which isn't started
#-errors with Gstreamer state transitions
#-current method in _rw_held/_ff_held of resetting the timer for another round
#only if the button is still down is not perfect. It is technically possible
#somebody could hit the button at exactly the right frequency so that the system
#would think it was down the whole time, but this is incredibly unlikely.

class NativityPlayer(object):
    """Implementation class of the music player."""

    def __init__(self, cfg):
        """Initializes the player. cfg is a ConfigParser.ConfigParser instance
        containing the player configuration."""

        self.log = logging.getLogger('nplayer')

        ## load up confguration

        self.invert_logic = cfg.getboolean('inputs', 'invert_logic')

        #pins which are handled by asynchronous callbacks
        self.pin_play = cfg.getint('inputs', 'pin_play')
        self.pin_stop = cfg.getint('inputs', 'pin_stop')
        self.pin_rw = cfg.getint('inputs', 'pin_rw')
        self.pin_ff = cfg.getint('inputs', 'pin_ff')
        self.pin_scene = cfg.getint('inputs', 'pin_scene')
        self._pins = (self.pin_play, self.pin_stop, self.pin_rw, self.pin_ff,
            self.pin_scene)

        #scene toggle pin is different; we don't care when it changes; we only
        #need to check its state at certain times
        self.pin_sctoggle = cfg.getint('inputs', 'pin_scene_toggle')

        #LCD color LED backlight pins
        self.pin_led_red = cfg.getint('lcd', 'pin_red')
        self.pin_led_green = cfg.getint('lcd', 'pin_green')
        self.pin_led_blue = cfg.getint('lcd', 'pin_blue')

        self.db_time = cfg.getint('inputs', 'db_time')
        self.libdir = cfg.get('fs', 'libdir')

        self.skip_hold_time = cfg.getfloat('prefs', 'skip_hold_time')
        self.skip_len = cfg.getint('prefs', 'skip_len')
        self.scp_span = cfg.getint('prefs', 'scp_span')
        self.scp_hits = cfg.getint('prefs', 'scp_hits')

        #flags for whether each input is high (True) or low (False), keyed by
        #pin number
        self._in_states = {
            self.pin_play: False,
            self.pin_stop: False,
            self.pin_rw: False,
            self.pin_ff: False,
            self.pin_scene: False,
        }

        #map for pin input handlers based on pin and state
        self._handler_map = {
            self.pin_play: { True: self._h_play_r, False: self._h_play_f },
            self.pin_stop: { True: self._h_stop_r, False: self._h_stop_f },
            self.pin_rw: { True: self._h_rw_r, False: self._h_rw_f },
            self.pin_ff: { True: self._h_ff_r, False: self._h_ff_f },
            self.pin_scene: { True: self._h_scene_r, False: self._h_scene_f },
        }

        #flag used for MP3 switching to ignore a play button release if we've
        #just switched MP3s (meaning the play button was pressed down as part
        #of the switch action, not because the user wants to start playing)
        self._ign_play = False

        #flag used for ignoring rewind/fast-forward events due to button
        #releases when we've just had a rewind/fast-forward due to that same
        #button being held down
        self._ign_rw = False
        self._ign_ff = False

        #timers for handling rewing/fast-forward button holds
        self._timer_ff = None
        self._timer_rw = None

        #list of times at which the scene button was released, to determine
        #whether we've receive three presses within the requisite time
        self._scp_times = []

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

        #length of current file, in nanoseconds; due to how Gstreamer works,
        #we can't obtain this info until the file has been loaded by Gstreamer
        self.cur_filelen = 0

        self.cur_file_base = os.path.basename(self.cur_file)

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

        #set up handle to LCD (not actually init'ing LCD yet)
        self.lcd = nhd_lcd.NHD_LCD(self.pin_led_red, self.pin_led_green,
            self.pin_led_blue)

        #event to provoke an LCD update
        self._upd_evt = threading.Event()

        #timer to provoke LCD updates for playing progress
        self._upd_timer = None


    def start(self):
        """Starts accepting input, then blocks forever."""

        ## set up pins

        #async pins
        for pin in self._pins:
            RPIO.add_interrupt_callback(pin, self._input_cb, edge='both',
                pull_up_down=(RPIO.PUD_UP if self.invert_logic else RPIO.PUD_DOWN),
                debounce_timeout_ms=self.db_time)

        #scene toggle
        RPIO.setup(self.pin_sctoggle, RPIO.IN,
            pull_up_down=(RPIO.PUD_UP if self.invert_logic else RPIO.PUD_DOWN))

        #set up LCD comms
        self.lcd.init()

        #start handling async events
        RPIO.wait_for_interrupts(threaded=True)

        #main LCD update loop
        self._upd_evt.set() #initial set to get a first printout
        while self._upd_evt.wait():

            self._upd_evt.clear()

            #wait out any Gstreamer state transition that may be happening on
            #the stream
            self.player.get_state(timeout=Gst.CLOCK_TIME_NONE)

            #default outputs is to say we're not currently playing
            con_msg = 'file %s' % self.cur_file
            lcd_line1 = self.cur_file_base
            lcd_line2 = 'stopped'
            lcd_leds = (0, 0, 0)

            if self.player.current_state == Gst.State.PLAYING:
                #player says that it's currently playing, but this does not
                #conclusively mean that the MP3 hasn't finished playing; we have
                #to drain out messages from the player bus to see if the stream
                #is actually done

                #handle any insteresting messages
                stream_end = False
                gmsg = self.pl_bus.pop()
                while gmsg is not None and not stream_end:
                    if gmsg.type == Gst.MessageType.EOS:
                        #finished playing
                        self.log.debug('got end of stream, resetting')
                        self.player.set_state(Gst.State.READY)
                        self.player.get_state(timeout=Gst.CLOCK_TIME_NONE)
                        stream_end = True
                    elif gmsg.type == Gst.MessageType.DURATION_CHANGED:
                        self.log.debug('stream duration changed')
                        self.cur_filelen =\
                            self.player.query_duration(Gst.Format.TIME)[1]

                    gmsg = self.pl_bus.pop()

                if not stream_end:
                    #output current position and playing status
                    cur_pos = self.player.query_position(Gst.Format.TIME)[1]
                    (cmins, csecs, cnsecs) = self._get_time(cur_pos)
                    (dmins, dsecs, dnsecs) = self._get_time(self.cur_filelen)
                    pct = float(cur_pos) / float(self.cur_filelen)

                    con_msg += ' (playing, %d:%.2d/%d:%.2d (%.2f %%))' %\
                        (cmins, csecs, dmins, dsecs, pct)
                    lcd_line2 = '%d:%.2d/%d:%.2d (play)' % (cmins, csecs, dmins,
                        dsecs)
                    lcd_leds = (1, 1, 1) #white backlight

                    #set the timer to do another output update since we are
                    #still playing
                    self._upd_timer = threading.Timer(0.5, self._trigger_update)
                    self._upd_timer.start()

            #output current status
            print con_msg
            self.lcd.overwrite(lcd_line1, lcd_line2)
            self.lcd.set_backlight(*lcd_leds)


    def _trigger_update(self):
        """Triggers an LCD update."""
        self._upd_evt.set()


    def _input_cb(self, pin, istate):
        """Callback for GPIO event detection.
        
        Context: callback thread"""

        if self.invert_logic:
            #inverted logic, button depressed represented by digital 0 (false)
            newState = not bool(istate)
        else:
            #straight logic, button depressed represented by digital 1 (true)
            newState = bool(istate)

        self._in_states[pin] = newState
        self._handler_map[pin][newState]()


    def _h_play_r(self):
        """Play pressed; nothing to do here."""
        pass


    def _h_play_f(self):
        """Play button released"""

        if True in (self._in_states[self.pin_rw], self._in_states[self.pin_ff]):
            #either rw or ff are pressed down, so this was a botched attempt
            #(on the user's part) to switch MP3 file
            self.log.warning(
                'botched switch file attempt (must release ff/rw first)')
        elif self._ign_play:
            #play is masked due to an MP3 switch
            self._ign_play = False
            return
        elif self.player.current_state != Gst.State.PLAYING:
            #a pure play button release, and we're not yet playing, so start
            self._play()
        #else: already playing, ignore


    def _h_stop_r(self):
        pass


    def _h_stop_f(self):
        """Stop button released, stop playing if currently playing."""
        if self.player.current_state == Gst.State.PLAYING:
            self.player.set_state(Gst.State.READY)
            self.player.get_state(timeout=Gst.CLOCK_TIME_NONE)
            self._upd_evt.set()

        #also cancel any fast-forward/rewind timers
        if self._timer_rw is not None:
            try:
                self._timer_rw.cancel()
            except Exception as e:
                log.debug('error canceling rw timer: %s', e)
        if self._timer_ff is not None:
            try:
                self._timer_ff.cancel()
            except Exception as e:
                log.debug('error canceling ff timer: %s', e)

        #also throw out saved scene play button press times
        self._scp_times = []


    def _h_rw_r(self):
        """Rewind button pressed; user may either be trying to rewind or
        cycle the MP3 to be played."""
        if self._in_states[self.pin_play]:
            #play is currently pressed, so this will be a request to change the
            #MP3 (on release), so we do nothing yet
            pass
        else:
            #play not pressed, so this is the start of a rewind command
            self._timer_rw = threading.Timer(self.skip_hold_time, self._rw_held)
            self._timer_rw.start()


    def _h_rw_f(self):
        """Rewind button released."""
        if self._in_states[self.pin_play]:
            #play is pressed, so this is an MP3 change
            if self.player.current_state == Gst.State.PLAYING:
                self.player.set_state(Gst.State.READY)
                self.player.get_state(timeout=Gst.CLOCK_TIME_NONE)

            self.cur_fileno = (self.cur_fileno - 1) % len(self.files)
            self.cur_file = self.files[self.cur_fileno]
            self.cur_file_base = os.path.basename(self.cur_file)
            self.player.set_property('uri', 'file://%s'%self.cur_file)
            self._upd_evt.set()
            self._ign_play = True
        else:
            #a rewind request
            if self._timer_rw is not None:
                self._timer_rw.cancel()
            if not self._ign_rw:
                self._skip_backward()
            else:
                self._ign_rw = False


    def _h_ff_r(self):
        """Fast-forward button pressed; may be start of either fast-forward or
        MP3 cycle."""
        if self._in_states[self.pin_play]:
            #play is pressed, so MP3 cycle is starting
            pass
        else:
            #play not pressed, so this is the start of a fast-forward
            self._timer_ff = threading.Timer(self.skip_hold_time, self._ff_held)
            self._timer_ff.start()


    def _h_ff_f(self):
        """Fast-forward button released."""
        if self._in_states[self.pin_play]:
            #play is pressed, so this is an MP3 change
            if self.player.current_state == Gst.State.PLAYING:
                self.player.set_state(Gst.State.READY)
                self.player.get_state(timeout=Gst.CLOCK_TIME_NONE)

            self.cur_fileno = (self.cur_fileno + 1) % len(self.files)
            self.cur_file = self.files[self.cur_fileno]
            self.cur_file_base = os.path.basename(self.cur_file)
            self.player.set_property('uri', 'file://%s'%self.cur_file)
            self._upd_evt.set()
            self._ign_play = True
        else:
            #a fast-forward request
            if self._timer_ff is not None:
                self._timer_ff.cancel()
            if not self._ign_ff:
                self._skip_forward()
            else:
                self._ign_ff = False


    def _h_scene_r(self):
        """Scene play button pressed."""
        pass

    def _h_scene_f(self):
        """Scene play button released."""
        now = time.time()

        if RPIO.input(self.pin_sctoggle):
            #scene play button is enabled

            self._scp_times.append(now)
            if len(self._scp_times) == self.scp_hits:
                #we have enough hits now
                if now - self._scp_times[0] <= float(self.scp_span):
                    #hits occurred within necessary timespan
                    if self.player.current_state != Gst.State.PLAYING:
                        self._play()

                    self._scp_times = []
                else:
                    #first hit was too old
                    self._scp_times.pop(0)
            #else: not enough hits yet


    def _play(self):
        """Begins playing the current file."""
        self.player.set_state(Gst.State.PLAYING)
        self.player.get_state(timeout=Gst.CLOCK_TIME_NONE)
        self.cur_filelen = self.player.query_duration(Gst.Format.TIME)[1]
        self._upd_evt.set()


    def _skip_forward(self):
        """Skips the playing track forward by the configured skip length."""
        if self.player.current_state == Gst.State.PLAYING:
            cur_pos = self.player.query_position(Gst.Format.TIME)[1]
            new_pos = max(0, cur_pos + self.skip_len*10**9)
            self.player.seek(1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH,
                Gst.SeekType.SET, new_pos, Gst.SeekType.NONE, -1)
            self.player.get_state(timeout=Gst.CLOCK_TIME_NONE)
            self._upd_evt.set()


    def _skip_backward(self):
        """Skips the playing track backward by the configured skip length."""
        if self.player.current_state == Gst.State.PLAYING:
            cur_pos = self.player.query_position(Gst.Format.TIME)[1]
            new_pos = max(0, cur_pos - self.skip_len*10**9)
            self.player.seek(1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH,
                Gst.SeekType.SET, new_pos, Gst.SeekType.NONE, -1)
            self.player.get_state(timeout=Gst.CLOCK_TIME_NONE)
            self._upd_evt.set()


    def _rw_held(self):
        """Handles the rewind button being held down."""
        self.log.info('rewind held')
        self._ign_rw = True
        self._skip_backward()

        if self._in_states[self.pin_rw]:
            #continue with another timer if the button is still down
            self._timer_rw = threading.Timer(self.skip_hold_time, self._rw_held)
            self._timer_rw.start()


    def _ff_held(self):
        """Handles the fast-forward button being help down."""
        self.log.info('fast-forward held')
        self._ign_ff = True
        self._skip_forward()

        if self._in_states[self.pin_ff]:
            self._timer_ff = threading.Timer(self.skip_hold_time, self._ff_held)
            self._timer_ff.start()


    @staticmethod
    def _get_time(nsecs):
        """Converts a number of nanoseconds into a tuple of (int) minutes, (int)
        seconds, and nanoseconds."""

        secs = int(nsecs / 10**9)
        lsecs = secs % 60
        mins = int((secs - lsecs) / 60)
        nsecs = nsecs % 10**9

        return (mins, lsecs, nsecs)
