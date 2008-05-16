#!/usr/bin/python

from distutils.core import setup, Extension

import callcontrol

macros  = [('MODULE_VERSION', '"%s"' % callcontrol.__version__)]

setup(name         = "callcontrol",
      version      = callcontrol.__version__,
      author       = "Dan Pascu",
      author_email = "dan@ag-projects.com",
      description  = "SIP call control",
      license      = "Commercial",
      platforms    = ["Platform Independent"],
      classifiers  = [
          #"Development Status :: 1 - Planning",
          #"Development Status :: 2 - Pre-Alpha",
          #"Development Status :: 3 - Alpha",
          "Development Status :: 4 - Beta",
          #"Development Status :: 6 - Mature",
          #"Development Status :: 7 - Inactive",
          "Intended Audience :: Service Providers",
          "License :: Commercial",
          "Operating System :: OS Independent",
          "Programming Language :: Python"
      ],
      packages     = ['callcontrol'],
      scripts      = ['call-control']
)
