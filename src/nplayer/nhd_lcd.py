"""Implements support for the NewHaven Display LCD, controlled via the native
I2C pins on the Raspberry Pi. Uses RPIO to control the colored backlight LEDs.
"""

import time
import smbus
import logging

import RPIO

class NHD_LCD(object):
    """Controls the NewHaven Display LCD via Raspberry Pi's native I2C support.
    """

    #I2C bus number to which the LCD is connected
    I2C_BUS = 1
    #address of the LCD
    DEV_ADDR = 0x3c

    #used to indicate whether bytes being sent are commands or data
    SEND_CMD = 0x00
    SEND_DATA = 0x40

    #commands
    CMD_CLEAR_DISP = 0x01 # Clear display
    CMD_HOME       = 0x02 # Set cursor at home (0,0)
    CMD_DISP_ON    = 0x0c # Turn display on
    CMD_DISP_OFF   = 0x08 # Turn display off
    CMD_SET_DDRAM  = 0x80 # Set DDRAM address
    CMD_CONTRAST   = 0x70 # Set LCD contrast

    def __init__(self, pin_red, pin_green, pin_blue):
        """Initializes the LCD controller class.

        Parameters:
            int pin_{red,green,blue}: pins which control the red, green, and
                blue backlight LEDs, respectively"""

        self.log = logging.getLogger('nplayer.nhd_lcd')
        self.bus = smbus.SMBus(self.I2C_BUS)

        self.pin_red = pin_red
        self.pin_green = pin_green
        self.pin_blue = pin_blue


    def init(self):
        """Initializes the LCD."""

        #initialize the LCD microcontroller
        self.log.info('initializing LCD communications')
        self.bus.write_byte_data(self.DEV_ADDR, self.SEND_CMD, 0x38)
        time.sleep(0.001)
        self.bus.write_byte_data(self.DEV_ADDR, self.SEND_CMD, 0x39)
        time.sleep(0.001)
        lcd_init_values = [0x14, 0x78, 0x5e, 0x6d, 0x0c, 0x01, 0x06]
        self.bus.write_i2c_block_data(self.DEV_ADDR, self.SEND_CMD,
            lcd_init_values)
        time.sleep(0.001)

        #set up the LED control pins
        self.log.debug('setting up LED control pins')
        for pin in (self.pin_red, self.pin_green, self.pin_blue):
            RPIO.setup(pin, RPIO.OUT)


    def _send_cmd(self, cmd):
        """Sends the given command (given as hex code) to the LCD."""
        self.bus.write_byte_data(self.DEV_ADDR, self.SEND_CMD, cmd)


    def clear(self):
        """Clears the LCD screen."""
        self._send_cmd(self.CMD_CLEAR_DISP)


    def home(self):
        """Returns the cursor to the home position (0,0)."""
        self._send_cmd(self.CMD_HOME)


    def set_cur_pos(self, row, col):
        """Sets the position of the cursor.

        Parameters:
            int row: zero-indexed row (zero is top); valid range: 0-1
            int col: zero-indexed column (zero is left); valid range: 0-19"""

        base = self.CMD_SET_DDRAM + col
        if row > 0:
            base += 0x40
        self._send_cmd(base)


    def write(self, text):
        """Displays the given text on the LCD. Text must be a single line
        and is limited to ASCII characters."""
        ordtext = [ord(letter) for letter in text]
        self.bus.write_i2c_block_data(self.DEV_ADDR, self.SEND_DATA, ordtext)


    def overwrite(self, line1, line2):
        """Clears the LCD, goes to home, and writes two lines of text."""
        self.clear()
        self.home()
        self.write(line1)
        self.set_cur_pos(1, 0)
        self.write(line2)


    def set_backlight(self, r, g, b):
        """Sets the state of the backlight LEDs.

        Parameters:
            bool r, g, b: enables or disables each colored backlight LED;
                combine the three primary colors to make other colors."""

        RPIO.output(self.pin_red, bool(r))
        RPIO.output(self.pin_green, bool(g))
        RPIO.output(self.pin_blue, bool(b))
