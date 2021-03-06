import calendar
import datetime
import json
import logging
import os.path
import random
import signal
import string
import sys
import threading
import time
import urllib2

import tornado.escape
import tornado.ioloop
import tornado.log
import tornado.options
import tornado.template
import tornado.web
import tornado.websocket
import validators

SIMULATION = False

try:
    from gpiozero import Button, DigitalOutputDevice
except ImportError:
    logging.basicConfig(level=logging.DEBUG)
    logging.warn("RUNNING IN SIMULATION MODE")
    SIMULATION = True


class Application(tornado.web.Application):
    """
    The main Application
    """
    _config = None
    _apikeys = None
    _slack = None

    def __init__(self):
        """
        Init method
        """
        handlers = [(r"/", MainHandler),
                    (r"/api/open/(.*)", ApiHandler),
                    (r"/door", DoorSocketHandler),
                    (r"/slack/(.*)", SlackHandler)]

        if SIMULATION:
            handlers.append((r"/simulation", SimulationHandler))

        settings = dict(
            cookie_secret=Application.config('webui.cookie.secret'),
            template_path=os.path.join(os.path.dirname(__file__), "templates"),
            static_path=os.path.join(os.path.dirname(__file__), "static"),
            xsrf_cookies=True,
        )
        Application.setup_hw_interface()
        super(Application, self).__init__(handlers, **settings)

    @classmethod
    def setup_hw_interface(cls):
        """
        Sets up the hardware interface with the before set config [set_config(config)].
        """
        try:
            if DoorSocketHandler.door is None:
                DoorSocketHandler.door = DigitalOutputDevice(int(Application.config('gpio.open')))

            if DoorSocketHandler.ring is None:
                DoorSocketHandler.ring = Button(int(Application.config('gpio.ring')), hold_time=0.25)
                DoorSocketHandler.ring.when_pressed = DoorSocketHandler.handle_ring
        except NameError:
            pass

    @classmethod
    def set_config(cls, config):
        """
        Sets the apikeys to be used by examined when checking if a api key
        is valid [valid_apikey(apikey=None)].

        :param config: dictionary with the application configuration.
        :type config: dict
        """
        Application._config = Application.__config__(config)

    @classmethod
    def set_apikeys(cls, apikeys):
        """
        Sets the apikeys to be used by examined when checking if a api key
        is valid [valid_apikey(apikey=None)].

        :param apikeys: dictionary with the apikeys read from apikeys.json
        :type apikey: dict
        """
        Application._apikeys = apikeys

    @classmethod
    def valid_apikey(cls, apikey=None):
        """
        Check if a given apikey is valid for opening the door and in case
        of usage of an one-time ("type": "once") apikey invalidating by
        recording the key in the file usedkeys.json.

        :param apikey: The apikey to test
        :type apikey: str
        :return: True is valid, False in invalid
        :rtype: bool
        """
        if apikey in Application._apikeys:

            api_key_type = Application._apikeys.get(apikey).get("type")

            if api_key_type == "master":
                return True
            else:
                # check if is Mo-Fr
                if datetime.datetime.today().weekday() > 4:
                    return False

                # check 07:00-18:00
                hour = datetime.datetime.today().hour
                if hour < 7 or hour > 18:
                    return False

                if api_key_type == "restricted":
                    return True

                # check if date is in range
                if datetime.datetime.strptime(Application._apikeys.get(apikey).get("from"), "%d.%m.%Y").date() \
                        <= datetime.datetime.today().date() \
                        <= datetime.datetime.strptime(Application._apikeys.get(apikey).get("till"), "%d.%m.%Y").date():

                    if api_key_type == "limited":
                        return True

                    elif api_key_type == "once":
                        try:
                            with open('usedkeys.json', 'r') as keys_file:
                                used = json.load(keys_file)
                        except IOError:
                            used = {}

                        if apikey in used:
                            logging.warn("one-time key already used.")
                            return False

                        used.update({"%s" % apikey: "%s" % time.time()})

                        with open('usedkeys.json', 'w') as keys_file:
                            json.dump(used, keys_file)

                        logging.warn("one-time key invalidated.")
                        return True

        return False


    @classmethod
    def config(cls, key=None):
        """
        Returns the value for a given key or when no key is given or
        key is not found, the whole config

        :param key: The key to lookup
        :return: The value or the config.
        :rtype: dict, str
        """
        try:
            return Application._config[key]
        except KeyError:
            return Application._config

    @classmethod
    def __config__(cls, _config):
        """
        Check essential config settings, use default if unset

        :param _config: dictionary with the application configuration.
        :type _config: dict
        """
        defaults = (("webui.port", "8080"),
                    ("webui.cookie.secret", "__TODO:_GENERATE_YOUR_OWN_RANDOM_VALUE__"),
                    ("door.name", "Door"),
                    ("door.open.timeout", "60"),
                    ("gpio.ring", "18"),
                    ("gpio.open", "23"))
        for setting, default in defaults:
            try:
                _config[setting]
            except KeyError:
                _config[setting] = default

        # Empty last ring and open, make persistent maybe later
        _config['_door.last.ring'] = ''
        _config['_door.last.open'] = ''

        if os.path.isfile('local_settings.json'):
            _config.update(load('local_settings.json'))

        return _config

    @classmethod
    def has_valid_slack_config(cls, _config):
        """
        Check essential config settings for Slack

        :param _config:
        :return: True is setup is valid, False otherwise
        """
        if Application._slack is None:
            Application._slack = False
            try:
                if not validators.url(_config["slack.webhook"]):
                    logging.warn("slack.webhook doesn't validate as URL")

                if not validators.url(_config["slack.baseurl"]):
                    logging.warn("slack.baseurl  doesn't validate as URL")

                if _config["slack.baseurl"][-1] == '/':
                    _config["slack.baseurl"] = _config["slack.baseurl"][0:-1]
                    logging.info("removed trailing '/' from slack.baseurl")

                Application._slack = True
            except KeyError:
                Application._slack = False
                logging.warn("Slack deactivated because minimum setup for Slack is incomplete or incorrect.")

        return Application._slack


class ApiHandler(tornado.web.RequestHandler):
    loader = None

    def get(self, apikey=None):
        """
        Handles get requests to /api/{apikey}

        :param apikey: Everything in query_path behind /api/
        :type apikey: str
        """

        if Application.valid_apikey(apikey):
            Application.config()['_door.last.open'] = "%s" % time.time()
            time.sleep(0.5)
            response = {'open': "%s" % time.time()}
            logging.info("API open for %s (%s)" % (apikey, self.request.remote_ip))
            try:
                DoorSocketHandler.door.on()
                time.sleep(0.5)
                DoorSocketHandler.door.off()
            except AttributeError, e:
                if SIMULATION:
                    pass
                else:
                    logging.fatal(str(e) + " :: DoorSocketHandler.door not initialized.")
        else:
            response = {'error': "Unauthorized"}
            self.set_status(401)

        self.set_header('Content-Type', 'text/json')
        self.write(response)
        self.finish()


class SlackHandler(tornado.web.RequestHandler):
    loader = None

    def get(self, secret=None):
        """
        Handles get request to /slack/{secret}

        :param secret: Everything in query_path behind /slack/
        :type secret: str
        """
        do_open = False
        if secret == Application.config().pop('_door.open.secret', None):
            payload = {
                "action": "open",
                "timestamp": "%s" % time.time()
            }
            DoorSocketHandler.send_update(tornado.escape.json_encode(payload))
            do_open = True
        else:
            logging.info("ignoring OPEN without correct ring secret")

        self.render("slack.html", config=Application.config(), opened=do_open)
        if do_open:
            DoorSocketHandler.handle_open()

    @classmethod
    def send(cls, text, open_link=None):
        """

        :param text: The message
        :param open_link: The open link
        :type text: str
        :type open_link: str
        """

        if Application.has_valid_slack_config(Application.config()):

            template_file = 'slack.json'
            try:
                message = SlackHandler.loader.load(template_file).generate(channel=Application.config('slack.channel'),
                                                                           username=Application.config('door.name'),
                                                                           text=text,
                                                                           open_link=open_link)
            except AttributeError:
                SlackHandler.loader = tornado.template.Loader(os.path.join(os.path.dirname(__file__), "templates"),
                                                              autoescape=None)
                message = SlackHandler.loader.load(template_file).generate(channel=Application.config('slack.channel'),
                                                                           username=Application.config('door.name'),
                                                                           text=text,
                                                                           open_link=open_link)
            try:
                req = urllib2.Request(Application.config('slack.webhook'),
                                      data=message,
                                      headers={'Content-Type': 'application/json'})
                response = urllib2.urlopen(req, timeout=10)
                logging.info("Slack responded: %s on message '%s'", response.getcode(), text)

            except IOError, e:
                if hasattr(e, 'code'):  # HTTPError
                    logging.info("HTTP Error Code: %s" % e.errno)
                elif hasattr(e, 'reason'):  # URLError
                    logging.info("can't connect, reason: %s" % e.message)
                else:
                    pass


class MainHandler(tornado.web.RequestHandler):
    """
        Handles request to /
    """

    def get(self):
        self.render("index.html", config=Application.config())


class SimulationHandler(tornado.web.RequestHandler):
    """
        Handles request to /
    """

    def get(self):
        self.render("simulation.html", config=Application.config())


class DoorSocketHandler(tornado.websocket.WebSocketHandler):
    waiters = set()

    door = None
    ring = None

    timeout_thread = None

    def __init__(self, *args, **kwargs):
        super(DoorSocketHandler, self).__init__(*args, **kwargs)

    def get_compression_options(self):
        # Non-None enables compression with default options.
        return {}

    def open(self):
        logging.info('Client IP: %s connected.' % self.request.remote_ip)
        DoorSocketHandler.waiters.add(self)

        message = {
            "action": "update",
            "last_open": "%s" % Application.config('_door.last.open'),
            "last_ring": "%s" % Application.config('_door.last.ring'),
            "timestamp": "%s" % time.time()
        }

        self.write_message(message)

    def on_close(self):
        logging.info('Client IP: %s disconnected.' % self.request.remote_ip)
        DoorSocketHandler.waiters.remove(self)

    def on_message(self, message):
        logging.info("got message %s from %s", message, self.request.remote_ip)
        payload = tornado.escape.json_decode(message)

        payload.update({
            "timestamp": "%s" % time.time()
        })

        if payload['action'] == "open":
            try:
                if payload['secret'] == Application.config().pop('_door.open.secret', None):
                    payload.pop('secret', None)
                    DoorSocketHandler.send_update(tornado.escape.json_encode(payload))
                    self.handle_open()
                else:
                    logging.info("ignoring OPEN without correct ring secret")
            except KeyError:
                logging.info("ignoring OPEN without any secret given")
        elif payload['action'] == "simulate_ring":
            if SIMULATION:
                DoorSocketHandler.handle_ring()

    @classmethod
    def handle_ring(cls):
        """
           Handle a ring event by enabling the _Open Door_ button for a given time
        """
        timestamp = time.time()

        try:
            if timestamp - float(Application.config('_door.last.open')) < 1.0:
                logging.info("RING too close to last open")
                return
            else:
                logging.info("handling RING")
        except ValueError:
            pass

        Application.config()['_door.last.ring'] = "%s" % timestamp

        if DoorSocketHandler.timeout_thread is None:
            # 1st ring: create secret
            secret = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(2)) + \
                     "%s" % calendar.timegm(time.gmtime())
            Application.config()['_door.open.secret'] = secret

            # Start a thread enabling open button for door.open.timeout seconds
            DoorSocketHandler.timeout_thread = TimeoutThread(timeout=int(Application.config('door.open.timeout')))
            DoorSocketHandler.timeout_thread.start()
        else:
            while True:
                # Follow-up ring: reuse existing secret
                if '_door.open.secret' in Application.config():
                    secret = Application.config('_door.open.secret')
                    break

            # Extend the time if another ring occurs
            DoorSocketHandler.timeout_thread.extend()

        payload = {
            "action": "ring",
            "secret": "%s" % secret,
            "timestamp": "%s" % timestamp
        }
        DoorSocketHandler.send_update(tornado.escape.json_encode(payload))

        if Application.has_valid_slack_config(Application.config()):
            open_link = "%s/slack/%s" % (Application.config('slack.baseurl'), secret)
            SlackHandler.send('@here DING DONG ... RING RING ... KNOCK KNOCK', open_link)
            time.sleep(60)

    @classmethod
    def handle_open(cls):
        """
           Handle a open event by disabling the _Open Door_ button and flipping the GPIO open pin
        """
        if DoorSocketHandler.timeout_thread is None:
            logging.info("ignoring OPEN without prior ring")
            return

        logging.info("handling OPEN")

        DoorSocketHandler.timeout_thread.stop()
        DoorSocketHandler.timeout_thread = None

        # TODO: put the on-sleep-off into a thread to mitigate impact on website delivery

        Application.config()['_door.last.open'] = "%s" % time.time()
        try:
            DoorSocketHandler.door.on()
        except AttributeError, e:
            if SIMULATION:
                pass
            else:
                logging.fatal(str(e) + " :: DoorSocketHandler.door not initialized.")

        if Application.has_valid_slack_config(Application.config()):
            SlackHandler.send('DoorPI has opened the door.')

        time.sleep(0.5)

        try:
            DoorSocketHandler.door.off()
        except AttributeError, e:
            if SIMULATION:
                pass
            else:
                logging.fatal(str(e))

    @classmethod
    def send_update(cls, message):
        logging.info("sending message to %d waiters", len(cls.waiters))
        for waiter in cls.waiters:
            try:
                waiter.write_message(message)
            except:
                logging.error("Error sending message", exc_info=True)


class TimeoutThread(threading.Thread):
    def __init__(self, timeout=60):
        """
        TimeoutThread initialisation

        :param timeout: Timeout in seconds
        :type timeout: int
        """
        threading.Thread.__init__(self)
        self.timeout = timeout
        self.finish = int(time.time())+timeout
        self.wait = True

    def run(self):
        """
        Wait for timeout to occur and stop
        """
        while self.wait and int(time.time()) < self.finish:
            time.sleep(1)
            logging.info("remaining time to open: %s seconds" % (self.finish - int(time.time())))
        if self.wait:
            DoorSocketHandler.send_update({"action": "timeout"})

    def extend(self):
        """
        Extends the timeout
        """
        self.finish += self.timeout

    def stop(self):
        """
        Lets the thread stop
        """
        self.wait = False


def load(filename):
    """
    Loads a JSON file and returns a dict.

    :param filename: The filename to load
    :type filename: str
    :return: A dictionary
    :rtype: dict
    """
    _dict = {}

    try:
        with open(filename, 'r') as settings_file:
            _dict = json.load(settings_file)
    except IOError:
        pass

    return _dict


def date_time_string(timestamp=None):
    """
    Return the current date and time formatted for a message header.

    :param timestamp:
    :return: a usable string representation for the human
    :rtype: str
    """

    weekdayname = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

    monthname = [None,
                 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    if timestamp is None:
        timestamp = time.time()
    year, month, day, hour, minute, second, weekday, yearday, dst = time.gmtime(timestamp)
    return_str = "%s, %02d %3s %4d %02d:%02d:%02d GMT" % (
        weekdayname[weekday],
        day, monthname[month], year,
        hour, minute, second)
    return return_str


def load_setup(signum=None, frame=None):
    """
    Reloads configuration on sigusr1 (systemctl reload doorpi-agent.service)

    :param signum: signature parameter
    :param frame: signature parameter
    """
    config = load('doorpi.json')
    Application.set_config(config)

    apikeys = load('apikeys.json')
    Application.set_apikeys(apikeys)


def handle_sigterm(signum=None, frame=None):
    """
    Saves current state on sigterm (systemctl stop doorpi-agent.service)
    end exists.

    :param signum: signature parameter
    :param frame: signature parameter
    """
    with open('doorpi_state.json', 'w') as settings_file:
        json.dump({
            "_door.last.open": "%s" % Application.config('_door.last.open'),
            "_door.last.ring": "%s" % Application.config('_door.last.ring')
        }, settings_file)
    tornado.ioloop.IOLoop.current().stop()
    SlackHandler.send('DoorPI stopped.')
    sys.exit(0)


def main():
    """
    Main entry point
    """
    logging.basicConfig(level=logging.INFO)

    signal.signal(signal.SIGUSR1, load_setup)
    signal.signal(signal.SIGTERM, handle_sigterm)

    load_setup()

    try:
        with open('doorpi_state.json', 'r') as settings_file:
            state = json.load(settings_file)
            Application.config()['_door.last.open'] = state['_door.last.open']
            Application.config()['_door.last.ring'] = state['_door.last.ring']
    except IOError:
        pass

    app = Application()

    app.listen(int(Application.config('webui.port')))

    if Application.has_valid_slack_config(Application.config()):
        SlackHandler.send('DoorPI started at %s' % Application.config('slack.baseurl'))

    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        SlackHandler.send('DoorPI stopped.')


if __name__ == "__main__":
    main()
