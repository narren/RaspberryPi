#!/usr/bin/python2.7
""" Garage Watch dog system

Author: Naren
"""

import logging
import re
import subprocess
import sys
import time
import traceback
from datetime import timedelta

import RPi.GPIO as GPIO

from smtp_support import send_mail
from twilio_support import call_phone
from twilio_support import send_sms

sys.path.append('/usr/local/etc')
import garage_watchdog_config as config

##############################################################################
# Sensor support
##############################################################################

def get_garage_door_state(pin):
    """Returns the state of the garage door on the specified pin as a string

    Args:
        pin: GPIO pin number.
    """
    if GPIO.input(pin): # pylint: disable=no-member
        state = 'open'
    else:
        state = 'closed'

    return state

def get_uptime():
    """Returns the uptime of the RPi as a string
    """
    with open('/proc/uptime', 'r') as uptime_file:
        uptime_seconds = int(float(uptime_file.readline().split()[0]))
        uptime_string = str(timedelta(seconds=uptime_seconds))
    return uptime_string

def get_gpu_temp():
    """Return the GPU temperature as a Celsius float
    """
    cmd = ['vcgencmd', 'measure_temp']

    measure_temp_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    output = measure_temp_proc.communicate()[0]

    gpu_temp = 'unknown'
    gpu_search = re.search('([0-9.]+)', output)

    if gpu_search:
        gpu_temp = gpu_search.group(1)

    return float(gpu_temp)

def get_cpu_temp():
    """Return the CPU temperature as a Celsius float
    """
    cpu_temp = 'unknown'
    with open("/sys/class/thermal/thermal_zone0/temp", "r") as temp_file:
        cpu_temp = float(temp_file.read()) / 1000.0

    return cpu_temp

def rpi_status():
    """Return string summarizing RPi status
    """
    return "CPU temp: %.1f, GPU temp: %.1f, Uptime: %s" % (get_gpu_temp(), get_cpu_temp(), get_uptime())

##############################################################################
# Logging and alerts
##############################################################################

def send_alerts(logger, recipients, subject, msg, state, time_in_state):
    """Send subject and msg to specified recipients

    Args:
        recipients: An array of strings of the form type:address
        subject: Subject of the alert
        msg: Body of the alert
        state: The state of the door
    """
    for recipient in recipients:
        if recipient[:4] == 'sms:':
            send_sms(recipient[4:], msg)
        elif recipient[:5] == 'call:':
            call_phone(recipient[5:])
        else:
            logger.error("Unrecognized recipient type: %s", recipient)

##############################################################################
# Misc support
##############################################################################

def truncate(input_str, length):
    """Truncate string to specified length

    Args:
        input_str: String to truncate
        length: Maximum length of output string
    """
    if len(input_str) < (length - 3):
        return input_str

    return input_str[:(length - 3)] + '...'

def format_duration(duration_sec):
    """Format a duration into a human friendly string"""
    days, remainder = divmod(duration_sec, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    ret = ''
    if days > 1:
        ret += "%d days " % (days)
    elif days == 1:
        ret += "%d day " % (days)

    if hours > 1:
        ret += "%d hours " % (hours)
    elif hours == 1:
        ret += "%d hour " % (hours)

    if minutes > 1:
        ret += "%d minutes" % (minutes)
    if minutes == 1:
        ret += "%d minute" % (minutes)

    if ret == '':
        ret += "%d seconds" % (seconds)

    return ret


def send_weekly_report():
    reminder_text = "Garage Door System working fine :)"
    today = time.strftime("%b-%d-%Y")
    subject = "Garage weekly report " + today
    send_mail(reminder_text)

    
##############################################################################
# Main functionality
##############################################################################
class GarageWatchdog(object):
    """Class with main function for Garage Watchdog system"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def main(self):
        """Main functionality
        """

        try:
            # Set up logging
            log_fmt = '%(asctime)-15s %(levelname)-8s %(message)s'
            log_level = logging.INFO

            if sys.stdout.isatty():
                # Connected to a real terminal - log to stdout
                logging.basicConfig(format=log_fmt, level=log_level)
            else:
                # Background mode - log to file
                logging.basicConfig(format=log_fmt, level=log_level, filename=config.LOG_FILENAME)

            # Banner
            self.logger.info("==========================================================")
            self.logger.info("Garage watch dog system starts")

            # Use Raspberry Pi board pin numbers
            self.logger.info("Configuring global settings")
            GPIO.setmode(GPIO.BOARD)

            # Configure the sensor pins as inputs with pull up resistors
            for door in config.GARAGE_DOORS:
                self.logger.info("Configuring pin %d for \"%s\"", door['pin'], door['name'])
                GPIO.setup(door['pin'], GPIO.IN, pull_up_down=GPIO.PUD_UP)

            # Last state of each garage door
            door_states = dict()

            # time.time() of the last time the garage door changed state
            time_of_last_state_change = dict()

            # Index of the next alert to send for each garage door
            alert_states = dict()

            # Read initial states
            for door in config.GARAGE_DOORS:
                name = door['name']
                state = get_garage_door_state(door['pin'])

                door_states[name] = state
                time_of_last_state_change[name] = time.time()
                alert_states[name] = 0

                self.logger.info("Initial state of \"%s\" is %s", name, state)

            status_report_countdown = 5
            # one week in seconds
            keep_alive_time = config.KEEP_ALIVE_MSG_DURATION
            while True:
                for door in config.GARAGE_DOORS:
                    name = door['name']
                    state = get_garage_door_state(door['pin'])
                    time_in_state = time.time() - time_of_last_state_change[name]

                    # Check if the door has changed state
                    if door_states[name] != state:
                        door_states[name] = state
                        time_of_last_state_change[name] = time.time()
                        self.logger.info("State of \"%s\" changed to %s after %.0f sec", name, state, time_in_state)

                        # Reset alert when door changes state
                        if alert_states[name] > 0:
                            # Use the recipients of the last alert
                            recipients = door['alerts'][alert_states[name] - 1]['recipients']
                            send_alerts(self.logger, recipients, name, "%s is now %s" % (name, state), state, 0)
                            alert_states[name] = 0

                        # Reset time_in_state
                        time_in_state = 0

                    # See if there are more alerts
                    if len(door['alerts']) > alert_states[name]:
                        # Get info about alert
                        alert = door['alerts'][alert_states[name]]

                        # Has the time elapsed and is this the state to trigger the alert?
                        if time_in_state > alert['time'] and state == alert['state']:
                            send_alerts(self.logger, alert['recipients'], name, "%s has been %s for %d seconds!" % (name, state, time_in_state), state, time_in_state)
                            alert_states[name] += 1

                # Periodically log the status for debug and ensuring RPi doesn't get too hot
                status_report_countdown -= 1
                if status_report_countdown <= 0:
                    status_msg = rpi_status()

                    for name in door_states:
                        status_msg += ", %s: %s/%d/%d" % (name, door_states[name], alert_states[name], (time.time() - time_of_last_state_change[name]))

                    self.logger.info(status_msg)

                    status_report_countdown = 600
                if(keep_alive_time <= 0):
                    send_weekly_report()
                    keep_alive_time = config.KEEP_ALIVE_MSG_DURATION
                else:
                    keep_alive_time -= 1

                # Poll every 1 second
                time.sleep(1)
        except KeyboardInterrupt:
            logging.critical("Terminating due to keyboard interrupt")
        except:
            logging.critical("Terminating due to unexpected error: %s", sys.exc_info()[0])
            logging.critical("%s", traceback.format_exc())

        GPIO.cleanup() # pylint: disable=no-member

if __name__ == "__main__":
    GarageWatchdog().main()
