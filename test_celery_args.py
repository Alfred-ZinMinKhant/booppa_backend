import sys
sys.argv.extend(['--without-mingle', '--without-gossip', '--without-heartbeat'])
from celery import Celery
app = Celery('test')
