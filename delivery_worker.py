"""Explicit approved-mail worker. No automatic startup, retry or external I/O on import."""
import argparse
import importlib
import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit

VERSION = '8.25.0'


class DeliveryNotSent(Exception):
    """Known failure before acceptance; safe to offer a reviewed retry."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def configuration(env=None):
    env = os.environ if env is None else env
    origin = env.get('BUILDCOMMAND_PUBLIC_URL', '').rstrip('/')
    try:
        url = urlsplit(origin)
        port_valid = url.port is None or 1 <= url.port <= 65535
    except ValueError:
        raise ValueError('Set a valid HTTPS construction app origin.') from None
    if (url.scheme != 'https' or not url.hostname or not port_valid or url.username or
            url.password or url.query or url.fragment or url.path or any(c.isspace() for c in origin)):
        raise ValueError('Set BUILDCOMMAND_PUBLIC_URL to the HTTPS origin of the construction app.')
    sender, host = env.get('BC_MAIL_FROM', ''), env.get('BC_SMTP_HOST', '')
    if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', sender) or not host or any(c.isspace() for c in host):
        raise ValueError('Set BC_MAIL_FROM and BC_SMTP_HOST.')
    try:
        port = int(env.get('BC_SMTP_PORT', '465'))
    except ValueError:
        raise ValueError('Use TLS SMTP port 465 or STARTTLS port 587.') from None
    if port not in {465, 587}:
        raise ValueError('Use TLS SMTP port 465 or STARTTLS port 587.')
    user, password = env.get('BC_SMTP_USER', ''), env.get('BC_SMTP_PASSWORD', '')
    if bool(user) != bool(password):
        raise ValueError('Set both BC_SMTP_USER and BC_SMTP_PASSWORD when authentication is required.')
    return dict(origin=origin, sender=sender, host=host, port=port, user=user, password=password)


def configuration_status(env=None):
    """Configuration presence only; exposes neither credentials nor live provider status."""
    try:
        configuration(env)
        return dict(configured=True, message='Email settings are present. A live approved send is still required to verify delivery.')
    except ValueError as exc:
        return dict(configured=False, message=str(exc))


def smtp_send(config, job):
    connection = None
    try:
        message = EmailMessage()
        message['From'], message['To'] = config['sender'], job['email']
        message['Subject'] = job['subject']
        message['Message-ID'] = '<' + job['message_key'] + '@' + config['sender'].split('@', 1)[1] + '>'
        message.set_content(job['body'] + '\n\n' + config['origin'] + '/workspace/transmittals/' +
                            str(job['package_id']) + '\n\nBuildCommand AI\nBuilt By Willy LaHood ©2026')
    except Exception:
        raise DeliveryNotSent('message_invalid') from None
    try:
        # No message body is submitted during connection, TLS or authentication.
        try:
            if config['port'] == 465:
                connection = smtplib.SMTP_SSL(config['host'], config['port'], context=ssl.create_default_context(), timeout=20)
            else:
                connection = smtplib.SMTP(config['host'], config['port'], timeout=20)
                connection.ehlo()
                connection.starttls(context=ssl.create_default_context())
                connection.ehlo()
            if config['user']:
                connection.login(config['user'], config['password'])
        except smtplib.SMTPAuthenticationError:
            raise DeliveryNotSent('authentication_failed') from None
        except Exception:
            raise DeliveryNotSent('connection_failed') from None
        try:
            refused = connection.send_message(message)
            if refused:
                raise DeliveryNotSent('recipient_rejected')
        except smtplib.SMTPRecipientsRefused:
            raise DeliveryNotSent('recipient_rejected') from None
        except smtplib.SMTPSenderRefused:
            raise DeliveryNotSent('sender_rejected') from None
        except smtplib.SMTPDataError:
            raise DeliveryNotSent('message_rejected') from None
        # Disconnects/timeouts while submitting are deliberately NOT safe-to-retry.
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass  # Successful acceptance is not undone by connection cleanup.


def eligible(b, c, job):
    row = c.execute('SELECT * FROM bc824_packages WHERE id=? AND company_id=? AND project_id=?',
                    (job['package_id'], job['company_id'], job['project_id'])).fetchone()
    if not row or row['acknowledged_at'] or row['expires'] <= b.now().isoformat():
        return False
    share = c.execute('SELECT * FROM bc_shared_work WHERE id=? AND company_id=? AND project_id=?',
                      (row['share_id'], job['company_id'], job['project_id'])).fetchone()
    if (not share or share['revoked_at'] or share['state'] != 'OPEN' or
            share['recipient_user_id'] != job['recipient_id'] or row['recipient_id'] != job['recipient_id']):
        return False
    actor = c.execute('SELECT id,company_id,email,role,display_name FROM users WHERE id=? AND company_id=?',
                      (row['created_by'], job['company_id'])).fetchone()
    if not actor:
        return False
    try:
        b.ns['_bc850_project'](c, dict(actor), job['project_id'])
        recipient = b.ns['_bc850_recipient'](c, dict(actor), job['project_id'], job['recipient_id'])
    except b.ns['_BC850_Problem']:
        return False
    return recipient['email'] == job['email'] == json.loads(row['snapshot_json'])['recipient']['email']


def heartbeat(b, finished=False):
    if 'bc824_worker_status' not in b.tables:
        return
    now = b.now().isoformat()
    # One service-wide heartbeat. Project users only see time and version, never other tenants' jobs.
    with b.db(True) as c:
        c.execute('''INSERT INTO bc824_worker_status(company_id,project_id,started,finished,worker_version)
            VALUES(0,0,?,?,?) ON CONFLICT(company_id,project_id) DO UPDATE SET
            started=excluded.started,finished=excluded.finished,worker_version=excluded.worker_version''',
            (now, now if finished else '', VERSION))


def run_once(b, config, send=smtp_send, limit=20):
    result = {'accepted_by_smtp': 0, 'skipped': 0, 'uncertain': 0, 'failed': 0}
    heartbeat(b)
    for _ in range(min(100, max(1, limit))):
        with b.db(True) as c:
            row = c.execute("SELECT * FROM bc824_mail WHERE state='queued' AND send_after<=? ORDER BY id LIMIT 1" +
                            b.field.lock, (b.now().isoformat(),)).fetchone()
            if not row:
                break
            job = dict(row)
            if not eligible(b, c, job):
                c.execute("UPDATE bc824_mail SET state='skipped',error_code='access_or_receipt_changed' WHERE id=?", (job['id'],))
                result['skipped'] += 1
                continue
            c.execute("UPDATE bc824_mail SET state='sending',attempted_at=? WHERE id=?", (b.now().isoformat(), job['id']))
        try:
            with b.db() as c:
                allowed = eligible(b, c, job)
            if not allowed:
                with b.db(True) as c:
                    c.execute("UPDATE bc824_mail SET state='skipped',error_code='access_changed' WHERE id=?", (job['id'],))
                result['skipped'] += 1
                continue
            send(config, job)
        except DeliveryNotSent as exc:
            with b.db(True) as c:
                c.execute("UPDATE bc824_mail SET state='failed',error_code=? WHERE id=?", (exc.code, job['id']))
            result['failed'] += 1
        except Exception:
            with b.db(True) as c:
                c.execute("UPDATE bc824_mail SET state='uncertain',error_code='delivery_not_confirmed' WHERE id=?", (job['id'],))
            result['uncertain'] += 1
        else:
            with b.db(True) as c:
                c.execute("UPDATE bc824_mail SET state='accepted_by_smtp',sent_at=?,error_code='' WHERE id=?", (b.now().isoformat(), job['id']))
            result['accepted_by_smtp'] += 1
    heartbeat(b, finished=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check-config', action='store_true', help='Validate settings without opening the app, database or mail connection.')
    mode.add_argument('--send-approved', action='store_true')
    args = parser.parse_args()
    if args.check_config:
        status = configuration_status()
        print(json.dumps(status))
        return 0 if status['configured'] else 1
    try:
        config = configuration()
    except ValueError as exc:
        parser.error(str(exc))
    app = importlib.import_module('full_app')
    print(json.dumps(run_once(app.app.state.connected_field, config)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
