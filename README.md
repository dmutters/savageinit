# savageinit
Savage Worlds Adventure Edition (SWADE) Initiative Tracker

# This is very much a work-in-progress.

# Installation & Use
This can run on any computer with the correct dependencies installed.
The exact names of the dependencies and how to install them will vary depending on the operating system.

This is being developed on an Ubuntu-based Linux distribution, and instructions are written accordingly.

## Dependencies
Python3
Flask

On Ubuntu:
sudo apt install python3 python3-flask

## Running The Application
python card_app.py

(Stop the application with CTRL-C)

## Using The Application
The application will be hosted on port 5000 over HTTP.  It is accessed using a web browser.
On the computer where it's hosted, go to: http://localhost:5000
Other computers, go to: http://<hostaddress>:5000

## GM Login
To make changes to initiative order, deal cards, etc., you must be logged in as the GM.
The hardcoded GM password is: gamemaster

IF YOU CHANGE THE PASSWORD, BE AWARE THAT IT IS BEING SENT "IN THE CLEAR".  DO NOT USE THE SAME PASSWORD FOR ANYTHING ELSE.

## Non-GM Users
Non-GM users do not need to log in.  They will only see the current initiative order.
