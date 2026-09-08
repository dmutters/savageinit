# savageinit
Savage Worlds Adventure Edition (SWADE) Initiative Tracker

# This is very much a work-in-progress.

# Installation & Use
This can run on any computer with the correct dependencies installed.
The exact names of the dependencies and how to install them will vary depending on the operating system.

This is being developed on an Ubuntu-based Linux distribution, and instructions are written accordingly.

## Dependencies
Python3, Python3-Flask, Redis, Python3-Redis

On Ubuntu:
sudo apt install python3 python3-flask redis python3-redis

## Running The Application
python3 savageinit.py

(Stop the application with CTRL-C)

## Using The Application
The application will be hosted on port 5000 over HTTP.  It is accessed using a web browser.

On the computer where it's hosted, go to: http://localhost:5000

Other computers, go to: http://\<hostaddress\>:5000

## GM Login
To make changes to initiative order, deal cards, etc., you must be logged in as the GM.

The hardcoded GM password is: gamemaster

You can change the password either by copying credentials.json.example to credentials.json and editing that file, or by running the application like this:

python3 savageinit.py -\-gm-password SomePassword

IF YOU CHANGE THE PASSWORD, BE AWARE THAT IT IS BEING SENT "IN THE CLEAR".  DO NOT USE THE SAME PASSWORD FOR ANYTHING ELSE.

## Non-GM Users
Non-GM users do not need to log in.  They will only see the current initiative order.

## SystemD Service
To run the application persistently in the background, follow the instructions found in savageinit.service.

## Docker
To run the application in Docker, install Docker Compose using these instructions:

https://docs.docker.com/compose/install/

Then, add your user to the "docker" group:

sudo usermod -aG docker YourUserName

Log out or reboot and then log back in.

Finally, build the images and start the containers:

cd /wherever/you/put/savaginit

docker compose up -d

To tear down the containers and delete the images, do:

docker compose down --rmi all

