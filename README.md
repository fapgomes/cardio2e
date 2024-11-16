# Overview

This program can be used to integrate secant cardio2e to a mqtt server.

# Installation
Clone the repo
```
cd /opt
sudo git clone https://github.com/fapgomes/cardio2e.git
```
Copy the sample config file, and put your own configurations
```
mkdir /opt/cardio2e/
cd /opt/cardio2e/
sudo cp cardio2e.conf-sample cardio2e.conf
```
Create the system file
```sudo vi /etc/systemd/system/cardio2e.service```
And add the following to this file:
```
[Unit]
Description=cardio2e
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/cardio2e/cardio2e.py
WorkingDirectory=/opt/cardio2e
StandardOutput=inherit
StandardError=inherit
Restart=always
User=openhab

[Install]
WantedBy=multi-user.target
```
Reload systemd daemon
```
sudo systemctl daemon-reload
```
Start the service
```
sudo systemctl start cardio2e
```
# for homeassistant audodiscover you need to fill this on the global config:
```
ha_discover_prefix = homeassistant
```
