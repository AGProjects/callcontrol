#!/usr/bin/python

import re
from distutils.core import setup


def get_version():
    return re.search(r"""__version__\s+=\s+(?P<quote>['"])(?P<version>.+?)(?P=quote)""", open('callcontrol/__init__.py').read()).group('version')

setup(name         = "callcontrol",
      version      = get_version(),
      author       = "Dan Pascu",
      author_email = "dan@ag-projects.com",
      description  = "SIP call control",
      license      = "GPL",
      platforms    = ["Platform Independent"],
      classifiers  = [
          #"Development Status :: 1 - Planning",
          #"Development Status :: 2 - Pre-Alpha",
          #"Development Status :: 3 - Alpha",
          #"Development Status :: 4 - Beta",
          "Development Status :: 5 - Production/Stable",
          #"Development Status :: 6 - Mature",
          #"Development Status :: 7 - Inactive",
          "Intended Audience :: Service Providers",
          "License :: GNU General Public License (GPL)",
          "Operating System :: OS Independent",
          "Programming Language :: Python"
      ],
      packages     = ['callcontrol','callcontrol.rating','callcontrol.rating.backends'],
      scripts      = ['call-control']
)
