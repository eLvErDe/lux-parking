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
    import sqlalchemy
    import sqlalchemy.ext.declarative
except ImportError:
    print("Missing Python module SQLAlchemy, please apt-get install python3-sqlalchemy")
    sys.exit(1)
try:
    import MySQLdb
except ImportError:
    print("Missing Python module MySQLdb, please apt-get install python3-mysqldb")
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
    argparser.add_argument('--url',   type=str, default='https://feed.vdl.lu/circulation/parking/feed.rss', help='Extract data from vdl.lu RSS stream')
    argparser.add_argument('--dburl', type=str, default='mysql://luxparking:luxparkling@localhost//luxparking?charset=utf8', help='SQLAlchemy URL to database')

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

    if os.getenv('NO_LOGS_TS', None) is not None:
        log_formatter='%(levelname)s %(module)s %(message)s'
    else:
        log_formatter='%(asctime)s %(levelname)s %(module)s %(message)s'
    out_hdlr.setFormatter(logging.Formatter(log_formatter))
    out_hdlr.setLevel(logging.INFO)
    logger.addHandler(out_hdlr)
    logger.info('Logger successfully initiliazed')

    # Relax requests module logging
    logging.getLogger('requests.packages.urllib3').setLevel(logging.WARNING)

    # HTTP requester object
    api = HttpRequester(url=config.url)

    # SQLAlchemy
    try:
        db = sqlalchemy.create_engine(config.dburl)
        db_base_model = sqlalchemy.ext.declarative.declarative_base()

        class ParkingLot(db_base_model):
            __tablename__ = 'lots'

            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, index=True)
            name = sqlalchemy.Column(sqlalchemy.String(50), nullable=False)
            lat = sqlalchemy.Column(sqlalchemy.Float, nullable=False)
            lon = sqlalchemy.Column(sqlalchemy.Float, nullable=False)
            price = sqlalchemy.Column(sqlalchemy.Text)
            info = sqlalchemy.Column(sqlalchemy.Text)

        class ParkingEntry(db_base_model):
            __tablename__ = 'entries'

            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, index=True)
            park_id = sqlalchemy.Column(sqlalchemy.Integer, sqlalchemy.ForeignKey('lots.id'), nullable=False, index=True)
            free = sqlalchemy.Column(sqlalchemy.Integer)
            total = sqlalchemy.Column(sqlalchemy.Integer)
            full = sqlalchemy.Column(sqlalchemy.Boolean)
            timestamp = sqlalchemy.Column(sqlalchemy.DateTime, nullable=False, index=True)

        ParkingLot.metadata.create_all(db)
        ParkingEntry.metadata.create_all(db)

    except Exception as e:
        logger.error('An error occurred while initializing database system: %s', e)
        sys.exit(2)

    while True:

        try:
            rss = api.poll()
            feed = feedparser.parse(rss)
            logger.info('New RSS feed received successfully')

            # Start database session
            Session = sqlalchemy.orm.sessionmaker(bind=db, autoflush=False)
            session = Session()

            for entry in feed.entries:

                try:
                    park_free  = None if entry['vdlxml_actuel']  == '' else int(entry['vdlxml_actuel'])
                    park_total = None if entry['vdlxml_total']   == '' else int(entry['vdlxml_total'])
                    park_full  = None if entry['vdlxml_complet'] == '' else bool(int(entry['vdlxml_complet']))
                    park_name  = entry['title']
                    # Lol at vdl.lu web team
                    if park_name == 'Beggen':
                        park_id = 999999
                    else:
                        park_id = int(entry['id'])
                    park_info  = entry['vdlxml_divers']
                    park_price = entry['vdlxml_paiement']
                    park_lat   = entry['vdlxml_localisationlatitude']
                    park_lat = float(park_lat) if park_lat else None  # Luxembourg Sud B has no information atm
                    park_long  = entry['vdlxml_localisationlongitude']
                    park_long = float(park_long) if park_long else None
                    if park_lat is None or park_long is None:
                        logger.warning("Parking %s has not lat/long information, probaly not yet usable", park_name)
                        continue
                    logger.info('Parking "%s(%d)": %s / %s', park_name, park_id, park_free, park_total)

                    db_lot = ParkingLot(id=park_id,
                                        name=park_name,
                                        lat=park_lat,
                                        lon=park_long,
                                        price=park_price,
                                        info=park_info)
                    db_entry = ParkingEntry(park_id=park_id,
                                          free=park_free,
                                          total=park_total,
                                          full=park_full,
                                          timestamp=datetime.datetime.now().replace(second=00))
                    session.merge(db_lot)
                    session.add(db_entry)

                except Exception as e:
                    try:
                        logger.exception('Processing error occurred when trying to handle parking "%s" data: %s', park_name, e)
                    except Exception as e:
                        logger.error('Processing error occurred when trying to handle unknown parking (even no title entry)')

            # Insert to db
            session.commit()

        # Will catch any successful HTTP request containing body_text and status_code
        # First one catches 4xx and 5xx, second one is homemade and catches non wanted success HTTP code
        except (requests.exceptions.HTTPError, UnexpectedHttpStatusCode) as e:
            try:
               logger.error('HTTP error occurred when trying query API: %r, status_code: %d, body message was: %r', e, e.response.status_code, e.response.text)
            except Exception as e:
                logger.error('HTTP error occurred when trying query API and I could not extract status_code and body message from it: %r', e)
        except Exception as e:
            logger.error('API call/handling failed with error: %r', e)
