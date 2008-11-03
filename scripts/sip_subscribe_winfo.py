#!/usr/bin/env python

import sys
import traceback
import string
import socket
import os
import atexit
import select
import termios
import signal
from collections import deque
from thread import start_new_thread, allocate_lock
from threading import Thread
from Queue import Queue
from optparse import OptionParser, OptionValueError
from time import sleep
from application.process import process
from application.configuration import *
from urllib2 import HTTPError, URLError

from pypjua import *
from pypjua.clients import enrollment

from pypjua.applications import ParserError
from pypjua.applications.watcherinfo import *
from pypjua.applications.policy import *
from pypjua.applications.presrules import *

from pypjua.clients.clientconfig import get_path
from pypjua.clients.lookup import *

from xcaplib.client import XCAPClient

class Boolean(int):
    def __new__(typ, value):
        if value.lower() == 'true':
            return True
        else:
            return False

class GeneralConfig(ConfigSection):
    _datatypes = {"listen_udp": datatypes.NetworkAddress}
    listen_udp = datatypes.NetworkAddress("any")


class AccountConfig(ConfigSection):
    _datatypes = {"sip_address": str, "password": str, "display_name": str, "outbound_proxy": IPAddressOrHostname, "xcap_root": str, "use_presence_agent": Boolean}
    sip_address = None
    password = None
    display_name = None
    outbound_proxy = None
    xcap_root = None
    use_presence_agent = True


process._system_config_directory = os.path.expanduser("~/.sipclient")
enrollment.verify_account_config()
configuration = ConfigFile("config.ini")
configuration.read_settings("General", GeneralConfig)


queue = Queue()
packet_count = 0
start_time = None
old = None
user_quit = True
lock = allocate_lock()
sip_uri = None
pending = deque()
winfo = None
xcap_client = None
prules = None
prules_etag = None
allow_rule = None
allow_rule_identities = None
block_rule = None
block_rule_identities = None
polite_block_rule = None
polite_block_rule_identities = None

def get_prules():
    global prules, prules_etag, allow_rule, block_rule, allow_rule_identities, block_rule_identities
    prules = None
    prules_etag = None
    allow_rule = None
    allow_rule_identities = None
    block_rule = None
    block_rule_identities = None
    try:
        doc = xcap_client.get('pres-rules')
    except URLError, e:
        if e.code != 404:
            print "Cannot obtain 'pres-rules' document: %s" % str(e)
        else:
            prules = PresRules()
    except HTTPError, e:
        print "Cannot obtain 'pres-rules' document: %s" % str(e)
    else:
        try:
            prules = PresRules.parse(doc)
        except ParserError, e:
            print "Invalid 'pres-rules' document: %s" % str(e)
        else:
            prules_etag = doc.etag
            # find each rule type
            for rule in prules:
                if rule.actions is not None:
                    for action in rule.actions:
                        if isinstance(action, SubHandling):
                            if action == 'allow':
                                if rule.conditions is not None:
                                    for condition in rule.conditions:
                                        if isinstance(condition, Identity):
                                            allow_rule = rule
                                            allow_rule_identities = condition
                                            break
                            elif action == 'block':
                                if rule.conditions is not None:
                                    for condition in rule.conditions:
                                        if isinstance(condition, Identity):
                                            block_rule = rule
                                            block_rule_identities = condition
                                            break
                            elif action == 'polite-block':
                                if rule.conditions is not None:
                                    for condition in rule.conditions:
                                        if isinstance(condition, Identity):
                                            polite_block_rule = rule
                                            polite_block_rule_identities = condition
                                            break
                            break

def allow_watcher(watcher):
    global prules, prules_etag, allow_rule, allow_rule_identities
    for i in xrange(3):
        if prules is None:
            get_prules()
        if prules is not None:
            if allow_rule is None:
                allow_rule_identities = Identity()
                allow_rule = Rule('pres_whitelist', conditions=Conditions([allow_rule_identities]), actions=Actions([SubHandling('allow')]),
                        transformations=Transformations([ProvideServices([AllServices()]), ProvidePersons([AllPersons()]), 
                            ProvideDevices([AllDevices()]), ProvideAllAttributes()]))
                prules.append(allow_rule)
            if str(watcher) not in allow_rule_identities:
                allow_rule_identities.append(IdentityOne(str(watcher)))
            try:
                res = xcap_client.put('pres-rules', prules.toxml(pretty_print=True), etag=prules_etag)
            except HTTPError, e:
                print "Cannot PUT 'pres-rules' document: %s" % str(e)
                prules = None
            else:
                prules_etag = res.etag
                print "Watcher %s is now allowed" % watcher
                break
        sleep(0.1)
    else:
        print "Could not allow watcher %s" % watcher

def block_watcher(watcher):
    global prules, prules_etag, block_rule, block_rule_identities
    for i in xrange(3):
        if prules is None:
            get_prules()
        if prules is not None:
            if block_rule is None:
                block_rule_identities = Identity()
                block_rule = Rule('pres_blacklist', conditions=Conditions([block_rule_identities]), actions=Actions([SubHandling('block')]),
                        transformations=Transformations())
                prules.append(block_rule)
            if str(watcher) not in block_rule_identities:
                block_rule_identities.append(IdentityOne(str(watcher)))
            try:
                res = xcap_client.put('pres-rules', prules.toxml(pretty_print=True), etag=prules_etag)
            except HTTPError, e:
                print "Cannot PUT 'pres-rules' document: %s" % str(e)
                prules = None
            else:
                prules_etag = res.etag
                print "Watcher %s is now denied" % watcher
                break
        sleep(0.1)
    else:
        print "Could not deny watcher %s" % watcher

def polite_block_watcher(watcher):
    global prules, prules_etag, polite_block_rule, polite_block_rule_identities
    for i in xrange(3):
        if prules is None:
            get_prules()
        if prules is not None:
            if polite_block_rule is None:
                polite_block_rule_identities = Identity()
                polite_block_rule = Rule('pres_polite_blacklist', conditions=Conditions([polite_block_rule_identities]), actions=Actions([SubHandling('polite-block')]),
                        transformations=Transformations())
                prules.append(polite_block_rule)
            if str(watcher) not in polite_block_rule_identities:
                polite_block_rule_identities.append(IdentityOne(str(watcher)))
            try:
                res = xcap_client.put('pres-rules', prules.toxml(pretty_print=True), etag=prules_etag)
            except HTTPError, e:
                print "Cannot PUT 'pres-rules' document: %s" % str(e)
                prules = None
            else:
                prules_etag = res.etag
                print "Watcher %s is now politely blocked" % watcher
                break
        sleep(0.1)
    else:
        print "Could not politely block authorization of watcher %s" % watcher

def handle_winfo(result):
    buf = ["Received NOTIFY:", "----"]
    self = 'sip:%s@%s' % (sip_uri.user, sip_uri.host)
    wlist = winfo[self]
    buf.append("Active watchers:")
    for watcher in wlist.active:
        buf.append("  %s" % watcher)
    buf.append("Terminated watchers:")
    for watcher in wlist.terminated:
        buf.append("  %s" % watcher)
    buf.append("Pending watchers:")
    for watcher in wlist.pending:
        buf.append("  %s" % watcher)
    buf.append("Waiting watchers:")
    for watcher in wlist.waiting:
        buf.append("  %s" % watcher)
    buf.append("----")
    queue.put(("print", '\n'.join(buf)))
    if result.has_key(self):
        for watcher in result[self]:
            if (watcher.status == 'pending' or watcher.status == 'waiting') and watcher not in pending and xcap_client is not None:
                pending.append(watcher)


def termios_restore():
    global old
    if old is not None:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)

def getchar():
    global old
    fd = sys.stdin.fileno()
    if os.isatty(fd):
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] = new[3] & ~termios.ICANON & ~termios.ECHO
        new[6][termios.VMIN] = '\000'
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
            if select.select([fd], [], [], None)[0]:
                return sys.stdin.read(10)
        finally:
            termios_restore()
    else:
        return os.read(fd, 10)

def event_handler(event_name, **kwargs):
    global start_time, packet_count, queue, do_pjsip_trace, winfo
    if event_name == "Subscription_state":
        if kwargs["state"] == "ACTIVE":
            #queue.put(("print", "SUBSCRIBE was successful"))
            pass
        elif kwargs["state"] == "TERMINATED":
            if kwargs.has_key("code"):
                queue.put(("print", "Unsubscribed: %(code)d %(reason)s" % kwargs))
            else:
                queue.put(("print", "Unsubscribed"))
            queue.put(("quit", None))
        elif kwargs["state"] == "PENDING":
            queue.put(("print", "Subscription is pending"))
    elif event_name == "Subscription_notify":
        if ('%s/%s' % (kwargs['content_type'], kwargs['content_subtype'])) in WatcherInfo.accept_types:
            try:
                result = winfo.update(kwargs['body'])
            except ParserError, e:
                queue.put(("print", "Got illegal winfo document: %s\n%s" % (str(e), kwargs['body'])))
            else:
                handle_winfo(result)
    elif event_name == "siptrace":
        if start_time is None:
            start_time = kwargs["timestamp"]
        packet_count += 1
        if kwargs["received"]:
            direction = "RECEIVED"
        else:
            direction = "SENDING"
        buf = ["%s: Packet %d, +%s" % (direction, packet_count, (kwargs["timestamp"] - start_time))]
        buf.append("%(timestamp)s: %(source_ip)s:%(source_port)d --> %(destination_ip)s:%(destination_port)d" % kwargs)
        buf.append(kwargs["data"])
        queue.put(("print", "\n".join(buf)))
    elif event_name != "log":
        queue.put(("pypjua_event", (event_name, kwargs)))
    elif do_pjsip_trace:
        queue.put(("print", "%(timestamp)s (%(level)d) %(sender)14s: %(message)s" % kwargs))

def read_queue(e, username, domain, password, display_name, route, xcap_root, expires, do_siptrace, do_pjsip_trace):
    global user_quit, lock, queue, sip_uri, winfo, xcap_client
    lock.acquire()
    try:
        sip_uri = SIPURI(user=username, host=domain, display=display_name)
        sub = Subscription(Credentials(sip_uri, password), sip_uri, 'presence.winfo', route=route, expires=expires)
        winfo = WatcherInfo()
        
        if xcap_root is not None:
            xcap_client = XCAPClient(xcap_root, '%s@%s' % (sip_uri.user, sip_uri.host), password=password, auth=None)
        print 'Retrieving current presence rules from %s' % xcap_root
        get_prules()
        print 'Allowed list:'
        if allow_rule_identities is not None:
            for identity in allow_rule_identities:
                print '\t%s' % identity
        print 'Blocked list:'
        if block_rule_identities is not None:
            for identity in block_rule_identities:
                print '\t%s' % identity
        print 'Polite-blocked list:'
        if polite_block_rule_identities is not None:
            for identity in polite_block_rule_identities:
                print '\t%s' % identity
    
        print 'Subscribing to "%s@%s" for the presence.winfo event, at %s:%d' % (sip_uri.user, sip_uri.host, route.host, route.port)
        sub.subscribe()
        
        while True:
            command, data = queue.get()
            if command == "print":
                print data
                if len(pending) > 0:
                    print "%s watcher %s wants to subscribe to your presence information. Press (a) for allow, (d) for deny or (p) for polite blocking:" % (pending[0].status.capitalize(), pending[0])
            if command == "pypjua_event":
                event_name, args = data
            if command == "user_input":
                key = data
                if len(pending) > 0:
                    if key == 'a':
                        watcher = pending.popleft()
                        allow_watcher(watcher)
                    elif key == 'd':
                        watcher = pending.popleft()
                        block_watcher(watcher)
                    elif key == 'p':
                        watcher = pending.popleft()
                        polite_block_watcher(watcher)
                    else:
                        print "Please select a valid choice. Press (a) to allow, (d) to deny, (p) to polite block"
                    if len(pending) > 0:
                        print "%s watcher %s wants to subscribe to your presence information. Press (a) for allow, (d) for deny or (p) for polite blocking:" % (pending[0].status.capitalize(), pending[0])
            if command == "eof":
                command = "end"
                want_quit = True
            if command == "end":
                try:
                    sub.unsubscribe()
                except:
                    pass
            if command == "quit":
                user_quit = False
                break
    except:
        user_quit = False
        traceback.print_exc()
    finally:
        e.stop()
        if not user_quit:
            os.kill(os.getpid(), signal.SIGINT)
        lock.release()

def do_subscribe(**kwargs):
    global user_quit, lock, queue, do_pjsip_trace
    ctrl_d_pressed = False
    do_pjsip_trace = kwargs["do_pjsip_trace"]
    outbound_proxy = kwargs.pop("outbound_proxy")
    if outbound_proxy is None:
        proxy_host, proxy_port, proxy_is_ip = kwargs["domain"], None, False
    else:
        proxy_host, proxy_port, proxy_is_ip = outbound_proxy
    try:
        kwargs["route"] = Route(*lookup_srv(proxy_host, proxy_port, proxy_is_ip, 5060))
    except RuntimeError, e:
        print e.message
        return

    e = Engine(event_handler, do_siptrace=kwargs['do_siptrace'], auto_sound=False, local_ip=kwargs.pop("local_ip"), local_port=kwargs.pop("local_port"))
    e.start()
    start_new_thread(read_queue, (e,), kwargs)
    atexit.register(termios_restore)
    
    try:
        while True:
            char = getchar()
            if char == "\x04":
                if not ctrl_d_pressed:
                    queue.put(("eof", None))
                    ctrl_d_pressed = True
            else:
                queue.put(("user_input", char))
    except KeyboardInterrupt:
        if user_quit:
            print "Ctrl+C pressed, exiting instantly!"
            queue.put(("quit", True))
        lock.acquire()
        return
    
def parse_outbound_proxy(option, opt_str, value, parser):
    try:
        parser.values.outbound_proxy = IPAddressOrHostname(value)
    except ValueError, e:
        raise OptionValueError(e.message)

def parse_options():
    retval = {}
    description = "This script displays the current presence rules, SUBSCRIBEs to the presence.winfo event of itself and prompts the user to update the presence rules document when a new watcher is in 'pending'/'waiting' state. The program will un-SUBSCRIBE and quit when CTRL+D is pressed."
    usage = "%prog [options]"
    parser = OptionParser(usage=usage, description=description)
    parser.print_usage = parser.print_help
    parser.add_option("-a", "--account-name", type="string", dest="account_name", help="The account name from which to read account settings. Corresponds to section Account_NAME in the configuration file. If not supplied, the section Account will be read.", metavar="NAME")
    parser.add_option("--sip-address", type="string", dest="sip_address", help="SIP address of the user in the form user@domain")
    parser.add_option("-p", "--password", type="string", dest="password", help="Password to use to authenticate the local account. This overrides the setting from the config file.")
    parser.add_option("-n", "--display-name", type="string", dest="display_name", help="Display name to use for the local account. This overrides the setting from the config file.")
    parser.add_option("-e", "--expires", type="int", dest="expires", help='"Expires" value to set in SUBSCRIBE. Default is 300 seconds.')
    parser.add_option("-o", "--outbound-proxy", type="string", action="callback", callback=parse_outbound_proxy, help="Outbound SIP proxy to use. By default a lookup of the domain is performed based on SRV and A records. This overrides the setting from the config file.", metavar="IP[:PORT]")
    parser.add_option("-x", "--xcap-root", type="string", dest="xcap_root", help = 'The XCAP root to use to access the pres-rules document for authorizing subscriptions to presence.')
    parser.add_option("-s", "--trace-sip", action="store_true", dest="do_siptrace", help="Dump the raw contents of incoming and outgoing SIP messages (disabled by default).")
    parser.add_option("-j", "--trace-pjsip", action="store_true", dest="do_pjsip_trace", help="Print PJSIP logging output (disabled by default).")
    options, args = parser.parse_args()
    
    if options.account_name is None:
        account_section = "Account"
    else:
        account_section = "Account_%s" % options.account_name
    if account_section not in configuration.parser.sections():
        raise RuntimeError("There is no account section named '%s' in the configuration file" % account_section)
    configuration.read_settings(account_section, AccountConfig)
    if not AccountConfig.use_presence_agent:
        raise RuntimeError("Presence is not enabled for this account. Please set presence=True in the config file")
    default_options = dict(expires=300, outbound_proxy=AccountConfig.outbound_proxy, sip_address=AccountConfig.sip_address, password=AccountConfig.password, display_name=AccountConfig.display_name, do_siptrace=False, do_pjsip_trace=False, xcap_root=AccountConfig.xcap_root, local_ip=GeneralConfig.listen_udp[0], local_port=GeneralConfig.listen_udp[1])
    options._update_loose(dict((name, value) for name, value in default_options.items() if getattr(options, name, None) is None))
    
    if not all([options.sip_address, options.password]):
        raise RuntimeError("No complete set of SIP credentials specified in config file and on commandline.")
    for attr in default_options:
        retval[attr] = getattr(options, attr)
    try:
        retval["username"], retval["domain"] = options.sip_address.split("@")
    except ValueError:
        raise RuntimeError("Invalid value for sip_address: %s" % options.sip_address)
    else:
        del retval["sip_address"]
    
    accounts = [(acc == 'Account') and 'default' or "'%s'" % acc[8:] for acc in configuration.parser.sections() if acc.startswith('Account')]
    accounts.sort()
    print "Accounts available: %s" % ', '.join(accounts)
    if options.account_name is None:
        print "Using default account: %s" % options.sip_address
    else:
        print "Using account '%s': %s" % (options.account_name, options.sip_address)

    return retval

def main():
    do_subscribe(**parse_options())

if __name__ == "__main__":
    try:
        main()
    except RuntimeError, e:
        print "Error: %s" % str(e)
        sys.exit(1)
