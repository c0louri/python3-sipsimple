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
import datetime
from thread import start_new_thread, allocate_lock
from threading import Thread
from Queue import Queue
from optparse import OptionParser, OptionValueError
from time import sleep
from application.process import process
from application.configuration import *
from pypjua import *
from pypjua.clients import enrollment

from pypjua.clients.lookup import *
from pypjua.clients.clientconfig import get_path

class GeneralConfig(ConfigSection):
    _datatypes = {"listen_udp": datatypes.NetworkAddress, "trace_pjsip": datatypes.Boolean, "trace_sip": datatypes.Boolean}
    listen_udp = datatypes.NetworkAddress("any")
    trace_pjsip = False
    trace_sip = False


class AccountConfig(ConfigSection):
    _datatypes = {"sip_address": str, "password": str, "display_name": str, "outbound_proxy": IPAddressOrHostname}
    sip_address = None
    password = None
    display_name = None
    outbound_proxy = None
    history_directory = '~/.sipclient/history'


class AudioConfig(ConfigSection):
    _datatypes = {"sample_rate": int, "echo_cancellation_tail_length": int,"codec_list": datatypes.StringList, "disable_sound": datatypes.Boolean}
    sample_rate = 32
    echo_cancellation_tail_length = 50
    codec_list = ["speex", "g711", "ilbc", "gsm", "g722"]
    disable_sound = False


process._system_config_directory = os.path.expanduser("~/.sipclient")
enrollment.verify_account_config()
configuration = ConfigFile("config.ini")
configuration.read_settings("Audio", AudioConfig)
configuration.read_settings("General", GeneralConfig)

queue = Queue()
packet_count = 0
start_time = None
old = None
user_quit = True
lock = allocate_lock()
do_trace_sip = False
trace_sip_file = None

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
    global start_time, packet_count, queue, do_trace_pjsip, do_trace_sip, trace_sip_file
    if event_name == "siptrace":
        if not do_trace_sip:
            return
        if trace_sip_file is None:
            try:
                filename = os.path.join(process._system_config_directory, 'log', '%s@%s' % (sip_uri.user, sip_uri.host), 'sip_trace.txt')
                trace_sip_file = open(filename, 'a')
            except IOError, e:
                queue.put(("print", "failed to create log file '%s'" % filename))
                return
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
        buf.append('--')
        trace_sip_file.write("\n".join(buf))
        trace_sip_file.flush()
    elif event_name != "log":
        queue.put(("pypjua_event", (event_name, kwargs)))
    elif do_trace_pjsip:
        queue.put(("print", "%(timestamp)s (%(level)d) %(sender)14s: %(message)s" % kwargs))

class RingingThread(Thread):

    def __init__(self, inbound):
        self.inbound = inbound
        self.stopping = False
        Thread.__init__(self)
        self.setDaemon(True)
        self.start()

    def stop(self):
        self.stopping = True

    def run(self):
        global queue
        while True:
            if self.stopping:
                return
            if self.inbound:
                queue.put(("play_wav", "ring_inbound.wav"))
            else:
                queue.put(("play_wav", "ring_outbound.wav"))
            sleep(5)


def read_queue(e, username, domain, password, display_name, route, target_username, target_domain, trace_sip, ec_tail_length, sample_rate, codecs, disable_sound, do_trace_pjsip, use_bonjour):
    global user_quit, lock, queue, do_trace_sip, sip_uri
    lock.acquire()
    inv = None
    audio = None
    ringer = None
    printed = False
    rec_file = None
    want_quit = target_username is not None
    other_user_agent = None
    do_trace_sip = trace_sip
    try:
        if not use_bonjour:
            sip_uri = SIPURI(user=username, host=domain, display=display_name)
            credentials = Credentials(sip_uri, password)
        if target_username is None:
            if use_bonjour:
                print "Using bonjour"
                print "Listening on local interface %s:%d" % (e.local_ip, e.local_port)
                print "Press Ctrl-D to quit, h to hang-up, r to toggle recording, < and > to adjust the echo cancellation"
                print 'Waiting for incoming SIP session requests...'
            else:
                reg = Registration(credentials, route=route)
                print 'Registering "%s" at %s:%d' % (credentials.uri, route.host, route.port)
                reg.register()
        else:
            inv = Invitation(credentials, SIPURI(user=target_username, host=target_domain), route=route)
            print "Call from %s to %s through proxy %s:%d" % (inv.caller_uri, inv.callee_uri, route.host, route.port)
            audio = AudioTransport(RTPTransport(e.local_ip))
            inv.set_offered_local_sdp(SDPSession(audio.transport.local_rtp_address, connection=SDPConnection(audio.transport.local_rtp_address), media=[audio.get_local_media()]))
            inv.set_state_CALLING()
            print "Press Ctrl-D to quit, h to hang-up, r to toggle recording, < and > to adjust the echo cancellation"
        while True:
            command, data = queue.get()
            if command == "print":
                print data
            if command == "pypjua_event":
                event_name, args = data
                if event_name == "Registration_state":
                    if args["state"] == "registered":
                        if not printed:
                            print "REGISTER was successful"
                            print "Contact: %s (expires in %d seconds)" % (args["contact_uri"], args["expires"])
                            if len(args["contact_uri_list"]) > 1:
                                print "Other registered contacts:\n%s" % "\n".join(["%s (expires in %d seconds)" % contact_tup for contact_tup in args["contact_uri_list"] if contact_tup[0] != args["contact_uri"]])
                            print "Press Ctrl-D to quit, h to hang-up, r to toggle recording, < and > to adjust the echo cancellation"
                            print "Waiting for incoming session..."
                            printed = True
                    elif args["state"] == "unregistered":
                        if args["code"] / 100 != 2:
                            print "Unregistered: %(code)d %(reason)s" % args
                        user_quit = False
                        command = "quit"
                elif event_name == "Invitation_state":
                    if args["prev_sdp_state"] != "DONE" and args["sdp_state"] == "DONE":
                        if args["obj"] is inv:
                            if args["sdp_negotiated"]:
                                audio.start(inv.get_active_local_sdp(), inv.get_active_remote_sdp(), 0)
                                e.connect_audio_transport(audio)
                                print 'Media negotiation done, using "%s" codec at %dHz' % (audio.codec, audio.sample_rate)
                                print "Audio RTP endpoints %s:%d <-> %s:%d" % (audio.transport.local_rtp_address, audio.transport.local_rtp_port, audio.transport.remote_rtp_address_sdp, audio.transport.remote_rtp_port_sdp)
                            else:
                                inv.set_state_DISCONNECTED()
                                print "SDP negotiation failed"
                    if args["state"] == "EARLY":
                        if "headers" in args and "User-Agent" in args["headers"]:
                            other_user_agent = args["headers"].get("User-Agent")
                        if ringer is None:
                            print "Ringing..."
                            ringer = RingingThread(target_username is None)
                    elif args["state"] == "INCOMING":
                        print "Incoming session..."
                        if inv is None:
                            remote_sdp = args["obj"].get_offered_remote_sdp()
                            if remote_sdp is not None and len(remote_sdp.media) == 1 and remote_sdp.media[0].media == "audio":
                                inv = args["obj"]
                                other_user_agent = args["headers"].get("User-Agent")
                                if ringer is None:
                                    ringer = RingingThread(True)
                                inv.set_state_EARLY()
                                print 'Incoming audio session from "%s", do you want to accept? (y/n)' % str(inv.caller_uri)
                            else:
                                print "Not an audio call, rejecting."
                                args["obj"].set_state_DISCONNECTED()
                        else:
                            print "Rejecting."
                            args["obj"].set_state_DISCONNECTED()
                    elif args["prev_state"] != "CONFIRMED" and args["state"] == "CONFIRMED":
                        if ringer is not None:
                            ringer.stop()
                            ringer = None
                            if other_user_agent is not None:
                                print 'Remote User Agent is "%s"' % other_user_agent
                    elif args["state"] == "DISCONNECTED":
                        if args["obj"] is inv:
                            if rec_file is not None:
                                rec_file.stop()
                                print 'Stopped recording audio to "%s"' % rec_file.file_name
                                rec_file = None
                            if ringer is not None:
                                ringer.stop()
                                ringer = None
                            if "code" in args and args["code"] / 100 != 2:
                                print "Session ended: %(code)d %(reason)s" % args
                                if args["code"] in [301, 302]:
                                    print "Received redirect request to %s" % args["headers"]["Contact"]
                            else:
                                print "Session ended"
                            if want_quit:
                                command = "unregister"
                            else:
                                audio = None
                                inv = None
            if command == "user_input":
                if inv is not None:
                    data = data[0]
                    if data.lower() == "h":
                        command = "end"
                        want_quit = target_username is not None
                    elif data in "0123456789*#ABCD" and audio is not None and audio.is_started:
                        audio.send_dtmf(data)
                    elif data.lower() == "r":
                        if rec_file is None:
                            src = '%s@%s' % (inv.caller_uri.user, inv.caller_uri.host)
                            dst = '%s@%s' % (inv.callee_uri.user, inv.callee_uri.host)
                            dir = os.path.join(os.path.expanduser(AccountConfig.history_directory), '%s@%s' % (username, domain))
                            try:
                                if not os.access(dir, os.F_OK):
                                    os.makedirs(dir)        
                                file_name = os.path.join(dir, '%s-%s-%s.wav' % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), src, dst))
                                rec_file = e.rec_wav_file(file_name)
                                print 'Recording audio to "%s"' % rec_file.file_name
                            except OSError, e:
                                print "Error while trying to record file: %s"
                        else:
                            rec_file.stop()
                            print 'Stopped recording audio to "%s"' % rec_file.file_name
                            rec_file = None
                    elif inv.state in ["INCOMING", "EARLY"]:
                        if data.lower() == "n":
                            command = "end"
                            want_quit = False
                        elif data.lower() == "y":
                            remote_sdp = inv.get_offered_remote_sdp()
                            audio = AudioTransport(RTPTransport(e.local_ip), remote_sdp, 0)
                            inv.set_offered_local_sdp(SDPSession(audio.transport.local_rtp_address, connection=SDPConnection(audio.transport.local_rtp_address), media=[audio.get_local_media()], start_time=remote_sdp.start_time, stop_time=remote_sdp.stop_time))
                            inv.set_state_CONNECTING()
                if data in ",<":
                    if ec_tail_length > 0:
                        ec_tail_length = max(0, ec_tail_length - 10)
                        e.auto_set_sound_devices(ec_tail_length)
                    print "Set echo cancellation tail length to %d ms" % ec_tail_length
                elif data in ".>":
                    if ec_tail_length < 500:
                        ec_tail_length = min(500, ec_tail_length + 10)
                        e.auto_set_sound_devices(ec_tail_length)
                    print "Set echo cancellation tail length to %d ms" % ec_tail_length
            if command == "play_wav":
                e.play_wav_file(get_path(data))
            if command == "eof":
                command = "end"
                want_quit = True
            if command == "end":
                try:
                    inv.set_state_DISCONNECTED()
                except:
                    command = "unregister"
            if command == "unregister":
                if target_username is None and not use_bonjour:
                    reg.unregister()
                else:
                    user_quit = False
                    command = "quit"
            if command == "quit":
                break
            data, args = None, None
    except:
        user_quit = False
        traceback.print_exc()
    finally:
        e.stop()
        if not user_quit:
            os.kill(os.getpid(), signal.SIGINT)
        if trace_sip_file is not None:
            trace_sip_file.close()
        lock.release()

def do_invite(**kwargs):
    global user_quit, lock, queue, do_trace_pjsip
    ctrl_d_pressed = False
    do_trace_pjsip = kwargs["do_trace_pjsip"]
    outbound_proxy = kwargs.pop("outbound_proxy")
    if kwargs["use_bonjour"]:
        kwargs["route"] = None
    else:
        if outbound_proxy is None:
            proxy_host, proxy_port, proxy_is_ip = kwargs["domain"], None, False
        else:
            proxy_host, proxy_port, proxy_is_ip = outbound_proxy
        try:
            kwargs["route"] = Route(*lookup_srv(proxy_host, proxy_port, proxy_is_ip, 5060))
        except RuntimeError, e:
            print e.message
            return
    e = Engine(event_handler, trace_sip=True, initial_codecs=kwargs["codecs"], ec_tail_length=kwargs["ec_tail_length"], sample_rate=kwargs["sample_rate"], auto_sound=not kwargs["disable_sound"], local_ip=kwargs.pop("local_ip"), local_port=kwargs.pop("local_port"))
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

def split_codec_list(option, opt_str, value, parser):
    parser.values.codecs = value.split(",")

def parse_options():
    retval = {}
    description = "This script can sit idle waiting for an incoming audio call, or perform an outgoing audio call to the target SIP account. The program will close the session and quit when Ctrl+D is pressed."
    usage = "%prog [options] [target-user@target-domain.com]"
    parser = OptionParser(usage=usage, description=description)
    parser.print_usage = parser.print_help
    parser.add_option("-a", "--account-name", type="string", dest="account_name", help="The account name from which to read account settings. Corresponds to section Account_NAME in the configuration file. If not supplied, the section Account will be read.", metavar="NAME")
    parser.add_option("--sip-address", type="string", dest="sip_address", help="SIP address of the user in the form user@domain")
    parser.add_option("-p", "--password", type="string", dest="password", help="Password to use to authenticate the local account. This overrides the setting from the config file.")
    parser.add_option("-n", "--display-name", type="string", dest="display_name", help="Display name to use for the local account. This overrides the setting from the config file.")
    parser.add_option("-o", "--outbound-proxy", type="string", action="callback", callback=parse_outbound_proxy, help="Outbound SIP proxy to use. By default a lookup of the domain is performed based on SRV and A records. This overrides the setting from the config file.", metavar="IP[:PORT]")
    parser.add_option("-s", "--trace-sip", action="store_true", dest="trace_sip", help="Dump the raw contents of incoming and outgoing SIP messages (disabled by default).")
    parser.add_option("-t", "--ec-tail-length", type="int", dest="ec_tail_length", help='Echo cancellation tail length in ms, setting this to 0 will disable echo cancellation. Default is 50 ms.')
    parser.add_option("-r", "--sample-rate", type="int", dest="sample_rate", help='Sample rate in kHz, should be one of 8, 16 or 32kHz. Default is 32kHz.')
    parser.add_option("-c", "--codecs", type="string", action="callback", callback=split_codec_list, help='Comma separated list of codecs to be used. Default is "speex,g711,ilbc,gsm,g722".')
    parser.add_option("-S", "--disable-sound", action="store_true", dest="disable_sound", help="Do not initialize the soundcard (by default the soundcard is enabled).")
    parser.add_option("-j", "--trace-pjsip", action="store_true", dest="do_trace_pjsip", help="Print PJSIP logging output (disabled by default).")
    options, args = parser.parse_args()

    retval["use_bonjour"] = options.account_name == "bonjour"
    if not retval["use_bonjour"]:
        if options.account_name is None:
            account_section = "Account"
        else:
            account_section = "Account_%s" % options.account_name
        if account_section not in configuration.parser.sections():
            raise RuntimeError("There is no account section named '%s' in the configuration file" % account_section)
        configuration.read_settings(account_section, AccountConfig)
    default_options = dict(outbound_proxy=AccountConfig.outbound_proxy, sip_address=AccountConfig.sip_address, password=AccountConfig.password, display_name=AccountConfig.display_name, trace_sip=GeneralConfig.trace_sip, ec_tail_length=AudioConfig.echo_cancellation_tail_length, sample_rate=AudioConfig.sample_rate, codecs=AudioConfig.codec_list, disable_sound=AudioConfig.disable_sound, do_trace_pjsip=GeneralConfig.trace_pjsip, local_ip=GeneralConfig.listen_udp[0], local_port=GeneralConfig.listen_udp[1])
    options._update_loose(dict((name, value) for name, value in default_options.items() if getattr(options, name, None) is None))

    if not retval["use_bonjour"]:
        if not all([options.sip_address, options.password]):
            raise RuntimeError("No complete set of SIP credentials specified in config file and on commandline.")
    for attr in default_options:
        retval[attr] = getattr(options, attr)
    try:
        if retval["use_bonjour"]:
            retval["username"], retval["domain"] = None, None
        else:
            retval["username"], retval["domain"] = options.sip_address.split("@")
    except ValueError:
        raise RuntimeError("Invalid value for sip_address: %s" % options.sip_address)
    else:
        del retval["sip_address"]
    if args:
        try:
            retval["target_username"], retval["target_domain"] = args[0].split("@")
        except ValueError:
            retval["target_username"], retval["target_domain"] = args[0], retval['domain']
    else:
        retval["target_username"], retval["target_domain"] = None, None
    accounts = [(acc == 'Account') and 'default' or "'%s'" % acc[8:] for acc in configuration.parser.sections() if acc.startswith('Account')]
    accounts.sort()
    print "Accounts available: %s" % ', '.join(accounts)
    if options.account_name is None:
        print "Using default account: %s" % options.sip_address
    else:
        if not retval["use_bonjour"]:
            print "Using account '%s': %s" % (options.account_name, options.sip_address)
    if retval['trace_sip']:
        print "Logging SIP trace to file '%s'" % os.path.join(process._system_config_directory, 'log', '%s@%s' % (retval["username"], retval["domain"]), 'sip_trace.txt')
    return retval

def main():
    do_invite(**parse_options())

if __name__ == "__main__":
    try:
        main()
    except RuntimeError, e:
        print "Error: %s" % str(e)
        sys.exit(1)
