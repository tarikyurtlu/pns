# -*- coding: utf-8 -*-

import logging
import pika
from datetime import timedelta
from flask.json import loads
from apns_clerk import APNs, Message, Session
from pns.utils import get_conf, get_logging_handler
from pns.models import db, Device


conf = get_conf()

# configure logger
logger = logging.getLogger(__name__)
logger.addHandler(get_logging_handler())
if conf.getboolean('application', 'debug'):
    logger.setLevel(logging.DEBUG)
else:
    logger.setLevel(logging.WARNING)

# APNS configuration
session = Session()
if conf.getboolean('application', 'debug'):
    con = session.get_connection("push_sandbox",
                                 cert_file=conf.get('apns', 'cert_sandbox'))
else:
    con = session.get_connection("push_production",
                                 cert_file=conf.get('apns', 'cert_production'))

# rabbitmq configuration
credentials = pika.credentials.PlainCredentials(
    username=conf.get('rabbitmq', 'username'),
    password=conf.get('rabbitmq', 'password'))
connection = pika.BlockingConnection(
    pika.ConnectionParameters(host=conf.get('rabbitmq', 'host'),
                              heartbeat_interval=conf.getint('rabbitmq', 'worker_heartbeat_interval'),
                              credentials=credentials))
channel = connection.channel()
channel.exchange_declare(exchange='pns_exchange', type='direct', durable=True)
channel.queue_declare(queue='pns_apns_queue', durable=True)
channel.queue_bind(exchange='pns_exchange', queue='pns_apns_queue', routing_key='pns_apns')


def callback(ch, method, properties, body):
    message = loads(body)
    logger.debug('payload: %s' % message)
    badge, sound, content_available = None, None, None
    if 'apns' in message['payload']:
        if 'badge' in message['payload']['apns']:
            badge = message['payload']['apns']['badge']
        if 'sound' in message['payload']['apns']:
            sound = message['payload']['apns']['sound']
        if 'content_available' in message['payload']['apns']:
            content_available = message['payload']['apns']['content_available']
    # time to live (in seconds)
    ttl = None
    if 'ttl' in message['payload']:
        ttl = timedelta(seconds=message['payload']['ttl'])
    message = Message(message['devices'],
                      alert=message['payload']['alert'],
                      badge=badge,
                      sound=sound,
                      content_available=content_available,
                      expiry=ttl,
                      extra=message['payload']['data'] if 'data' in message['payload'] else None)
    srv = APNs(con)
    try:
        res = srv.send(message)
    except Exception as ex:
        ch.basic_nack()
        logger.exception(ex)
        return
    # Check failures. Check codes in APNs reference docs.
    for token, reason in res.failed.items():
        code, errmsg = reason
        # according to APNs protocol the token reported here
        # is garbage (invalid or empty), stop using and remove it.
        logger.info('delivery failure apns_token: %s, reason: %s' % (token, errmsg))
        device_obj = Device.query.filter_by(platform_id=token).first()
        if device_obj:
            db.session.delete(device_obj)
    try:
        db.session.commit()
    except Exception as ex:
        db.session.rollback()
        logger.exception(ex)
    # Check failures not related to devices.
    for code, errmsg in res.errors:
        logger.error(errmsg)
    # Check if there are tokens that can be retried
    if res.needs_retry():
        # repeat with retry_message or reschedule your task
        srv.send(res.retry())
    ch.basic_ack(delivery_tag=method.delivery_tag)


channel.basic_qos(prefetch_count=1)
channel.basic_consume(callback, queue='pns_apns_queue')
channel.start_consuming()