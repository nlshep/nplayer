"""Main functionality of the music player."""

import time
import logging
import os

from RPi import GPIO
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

        #pre-load list of files
        self.files =\
            [os.path.join(self.libdir, x) for x in os.listdir(self.libdir)]
        if not self.files:
            raise Exception('no files in library dir %s' % self.libdir)

        if cfg.has_option('fs', 'def_file'):
            def_file = os.path.join(self.libdir, cfg.get('fs', 'def_file'))
            if os.path.isfile(def_file):
                self.cur_file = def_file
            else:
                self.cur_file = self.files[0]
        else:
            self.cur_file = self.files[0]
        self.log.info('chose default file %s', self.cur_file)

        #set up GStreamer
        GObject.threads_init()
        Gst.init(None)
        self.log.info('gstreamer initialized')

        #set up file player
        self.player = Gst.ElementFactory.make('playbin', 'player')
        self.player.set_property('uri', 'file://%s'%self.cur_file)
        self.log.info('player initialized')


    def start(self):
        """Starts accepting input, then blocks forever."""

        GPIO.setmode(GPIO.BCM)

        #set up pins
        for pin in self._pins:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
            GPIO.add_event_detect(pin, GPIO.BOTH,
                bouncetime=self.db_time, callback=self._input_cb)

        while True:
            time.sleep(10)


    def _input_cb(self, pin):
        """Callback for GPIO event detection."""
        istate = GPIO.input(pin)

        if istate: #button pressed
            self._in_states[pin] = True
            return
        else: #button released
            self._in_states[pin] = False

        if pin == self.pin_play:
            self.log.info('PLAY')
