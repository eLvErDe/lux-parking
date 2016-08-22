#!/usr/bin/python3

import sys
import time
import datetime
import logging
import os, shutil
try:
    import requests
except ImportError:
    print("Missing Python3 module requests, please apt-get install python3-requests")
    sys.exit(1)
try:
    import feedparser
except ImportError:
    print("Missing Python3 module feedparser, please apt-get install python3-feedparser")
    sys.exit(1)
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter, FileType
from timeout import timeout # Real timeout decorator using a thread

def parse_args():
    """Parse command line arguments and return config object."""

    # Raise terminal size, See https://bugs.python.org/issue13041
    os.environ['COLUMNS'] = str(shutil.get_terminal_size().columns)
    argparser = ArgumentParser(description='Luxembourg parking lot data poller', formatter_class=ArgumentDefaultsHelpFormatter)
    argparser.add_argument('--url',         type=str, default='http://service.vdl.lu/rss/circulation_guidageparking.php', help='Extract data from vdl.lu RSS stream')

    return argparser.parse_args()

class UnexpectedHttpStatusCode(requests.RequestException):
    """An unexpected HTTP status code has been received."""

    # See: https://github.com/kennethreitz/requests/blob/master/requests/exceptions.py
    # See: https://github.com/kennethreitz/requests/blob/master/requests/models.py#L848 (raise_for_status func)

class HttpRequester(object):
    """Wrapper class around requests module"""

    def __init__(self, *, url):
        self.url = url
        self.user_agent = 'Lux-Parking Poller'
        self.last_datetime = datetime.datetime.now()

    @timeout(5)
    def get(self):
        """Request API and return RSS XML as str"""

        headers = { 'User-Agent': self.user_agent }
        response = requests.get(self.url, headers=headers)

        # Will raise requests.exceptions.HTTPError if response code is 4xx or 5xx
        response.raise_for_status()

        # Manually raise HTTP exception for other ones
        if response.status_code != 200:
            http_error_msg = 'Unexpected HTTP status code %s for url: %s' % (response.status_code, response.url)
            raise UnexpectedHttpStatusCode(http_error_msg, response=response)

        self.last_datetime = datetime.datetime.now()
        return response.text

    def poll(self):
        """Make sure each call will be done every minute at same second"""

        # Wait next min:00
        now = datetime.datetime.now()
        start_time = now.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)

        while now < start_time:
            time.sleep(0.1)
            now = datetime.datetime.now()

        self.last_datetime = now

        return self.get()


if __name__ == '__main__':

    # Get command line arguments
    config =  parse_args()

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    out_hdlr = logging.StreamHandler(sys.stdout)
    out_hdlr.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(module)s %(message)s'))
    out_hdlr.setLevel(logging.INFO)
    logger.addHandler(out_hdlr)
    logger.info('Logger successfully initiliazed')

    # Relax requests module logging
    logging.getLogger('requests.packages.urllib3').setLevel(logging.WARNING)

    # HTTP requester object
    api = HttpRequester(url=config.url)

    while True:

        try:
            rss = api.poll()
            feed = feedparser.parse(rss)
            logger.info('New RSS feed received successfully')
            for entry in feed.entries:

                try:
                    park_used  = None if entry['vdlxml_actuel']  == '' else int(entry['vdlxml_actuel'])
                    park_total = None if entry['vdlxml_total']   == '' else int(entry['vdlxml_total'])
                    park_full  = None if entry['vdlxml_complet'] == '' else bool(entry['vdlxml_actuel'])
                    park_name  = entry['title']
                    # Lol at vdl.lu web team
                    if park_name == 'Beggen':
                        park_id = 0
                    else:
                        park_id = int(entry['id'])
                    park_info  = entry['vdlxml_divers']
                    park_price = entry['vdlxml_paiement']
                    park_lat   = float(entry['vdlxml_localisationlatitude'])
                    park_long  = float(entry['vdlxml_localisationlongitude'])
                    logger.info('Parking "%s(%d)": %s / %s)', park_name, park_id, park_used, park_total)

                except Exception as e:
                    try:
                        logger.error('Processing error occurred when trying to handle parking "%s" data', park_name)
                    except Exception as e:
                        logger.error('Processing error occurred when trying to handle unknown parking (even no title entry)')


        # Will catch any successful HTTP request containing body_text and status_code
        # First one catches 4xx and 5xx, second one is homemade and catches non wanted success HTTP code
        except (requests.exceptions.HTTPError, UnexpectedHttpStatusCode) as e:
            try:
               logger.error('HTTP error occurred when trying query API: %r, status_code: %d, body message was: %r', e, e.response.status_code, e.response.text)
            except Exception as e:
                logger.error('HTTP error occurred when trying query API and I could not extract status_code and body message from it: %r', e)
        except Exception as e:
            logger.error('API call/handling failed with error: %r', e)
