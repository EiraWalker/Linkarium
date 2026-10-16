#!/usr/bin/env python
import os
import sys

debug_auth = "--debug" in sys.argv
if debug_auth:
    sys.argv.remove("--debug")
os.environ["BOOKMARK_AUTH_DEBUG"] = "1" if debug_auth else "0"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
from django.core.management import execute_from_command_line

execute_from_command_line(sys.argv)
