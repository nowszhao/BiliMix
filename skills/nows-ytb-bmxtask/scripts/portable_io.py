"""UTF-8 console output for scripts launched from any OS/Agent shell."""
import sys


def configure_utf8():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
