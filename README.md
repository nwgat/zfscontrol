# ZFScontrol
webgui for zpool and zfs

# Install on Ubuntu 25.04
* `apt install nano python3 python3-flask zfsutils-linux util-linux git`
* add your user to sudo group
* `git clone https://github.com/nwgat/zfscontrol/ && cd zfscontrol`
* `python3 app.py`


# Install on Alpine (might not work correctly)
* `apk add nano sudo python3 py3-flask zfs lsblk git`
* `git clone https://github.com/nwgat/zfscontrol/ && cd zfscontrol`
* add your user to sudo group
* `python3 app.py`
