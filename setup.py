#!/usr/bin/python

import callcontrol
from distutils.core import setup


setup(
    name='callcontrol',
    version=callcontrol.__version__,

    description='SIP call control engine',
    long_description='Call control engine for OpenSIPS',
    url='http://callcontrol.ag-projects.com',

    license='GPLv2',
    author='AG Projects',
    author_email='support@ag-projects.com',

    platforms=['Platform Independent'],
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Service Providers',
        'License :: GNU General Public License 2 (GPLv2)',
        'Operating System :: OS Independent',
        'Programming Language :: Python'
    ],

    data_files=[('/etc/callcontrol', ['config.ini.sample'])],
    packages=['callcontrol', 'callcontrol.rating', 'callcontrol.rating.backends'],
    scripts=['call-control', 'call-control-cli']
)
