# Copyright (C) 2005-2008 AG-Projects. See LICENSE for details.
#

"""SIP Callcontrol"""

__version__ = "2.0.8"

configuration_filename = 'config.ini'
backup_calls_file = 'calls.dat'

package_requirements = {'python-application': '1.1.5'}

try:
    from application.dependency import ApplicationDependencies
except:
    class DependencyError(Exception): pass
    class ApplicationDependencies(object):
        def __init__(self, *args, **kwargs):
            pass
        def check(self):
            raise DependencyError("need python-application version %s or higher but it's not installed" % package_requirements['python-application'])

dependencies = ApplicationDependencies(**package_requirements)


