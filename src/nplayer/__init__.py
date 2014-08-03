"""FCC Living Nativity Raspberry PI music player software."""

import os

#location of default config file
DEF_CFG =\
os.path.join(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(
                __file__
            )
        )
    ),
    'player.conf'
)
