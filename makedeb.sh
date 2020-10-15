#!/bin/bash
if [ -f dist ]; then
    rm -r dist
fi


python3 setup.py sdist
sleep 2

cd dist
tar zxvf *.tar.gz

cd callcontrol-?.?.?

debuild --no-sign

ls

