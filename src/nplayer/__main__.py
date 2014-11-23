import argparse
import logging
import sys
import ConfigParser

from . import player, DEF_CFG

parser = argparse.ArgumentParser(description='Nativity scene music player')

parser.add_argument('-c', '--config', help='path to config file',
    default=DEF_CFG)
parser.add_argument('-v', '--verbose', action='store_const',
    default=logging.INFO, const=logging.DEBUG, dest='loglev')
#parser.add_argument('-l', '--logfile', help='path to log file')

args = parser.parse_args()

#set up logging
logging.basicConfig(stream=sys.stdout, level=args.loglev,
    format='[%(asctime)s] [%(levelname)3s] %(message)s')
#log = logging.getLogger('nplayer')
#log.addHandler(logging.StreamHandler())
#if args.logfile is not None:
#    log.addHandler(logging.FileHandler(args.logfile))

cfg = ConfigParser.ConfigParser()
if args.config not in cfg.read(args.config):
    print >>sys.stderr, '!! failed to load config file %s' % args.config
    sys.exit(1)

player_inst = player.NativityPlayer(cfg)
player_inst.start()
