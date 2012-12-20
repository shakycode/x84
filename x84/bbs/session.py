# -*- coding: utf-8 -*-
"""
Session engine for x/84, http://github.com/jquast/x84/
"""
import traceback
import threading
import logging
import struct
import math
import time
import imp
import sys
import os
import io

import x84.bbs.userbase
import x84.bbs.cp437
import x84.bbs.ini

SESSION = None
# TTYREC_UCOMPRESS = 15000
TTYREC_UCOMPRESS = None  # disabled for ttyplay -p(eek)
TTYREC_HEADER = unichr(27) + u'[8;%d;%dt'
TTYREC_ROTATE = 4
TTYREC_PADD = 10


def getsession():
    """
    Return session, after a .run() method has been called on any 1 instance.
    """
    return SESSION


def getterminal():
    """
    Return blessings terminal instance of this session.
    """
    return getsession().terminal


class Session(object):
    """
    A BBS Session engine, started by .run().
    """
    # pylint: disable=R0902,R0904
    #        Too many instance attributes (29/7)
    #        Too many public methods (25/20)

    def __init__(self, terminal, pipe, source, env):
        """
        Instantiate a Session instanance, only one session may be instantiated
        per process. Arguments:
            terminal: blessings.Terminal,
            pipe: multiprocessing.Pipe child end
            source: origin of the connect (ip, port),
            env: dict of environment variables, such as 'TERM', 'USER'.
        """
        # pylint: disable=W0603
        #        Using the global statement
        global SESSION
        assert SESSION is None, 'Session may be instantiated only once'
        SESSION = self
        self.pipe = pipe
        self.terminal = terminal
        self.source = source
        self.env = env
        self.lock = threading.Lock()
        self._user = None
        self._script_stack = [(x84.bbs.ini.CFG.get('matrix', 'script'),)]
        self._tap_input = x84.bbs.ini.CFG.getboolean('session', 'tap_input')
        self._tap_output = x84.bbs.ini.CFG.getboolean('session', 'tap_output')
        self._ttyrec_folder = x84.bbs.ini.CFG.get('system', 'ttyrecpath')
        self._record_tty = x84.bbs.ini.CFG.getboolean('session', 'record_tty')
        self._script_module = None
        self._fp_ttyrec = None
        self._ttyrec_fname = None
        self._connect_time = time.time()
        self._last_input_time = time.time()
        self._enable_keycodes = True
        self._activity = u'<uninitialized>'
        self._source = '<undefined>'
        self._encoding = 'utf8'
        self._recording = False
        # event buffer
        self._buffer = dict()
        # save state for ttyrec compression
        self._ttyrec_sec = -1
        self._ttyrec_usec = -1
        self._ttyrec_len_text = 0

    @property
    def duration(self):
        """
        Return length of time since connection began (float).
        """
        return time.time() - self._connect_time

    @property
    def connect_time(self):
        """
        Return time when connection began (float).
        """
        return self._connect_time

    @property
    def last_input_time(self):
        """
        Return last time of keypress (epoch float).
        """
        return self._last_input_time

    @property
    def idle(self):
        """
        Return length of time since last keypress occured (float).
        """
        return time.time() - self._last_input_time

    @property
    def activity(self):
        """
        Current activity (arbitrarily set).
        """
        return self._activity

    @activity.setter
    def activity(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        if self._activity != value:
            logger = logging.getLogger()
            logger.debug('activity=%s', value)
            self._activity = value

    @property
    def handle(self):
        """
        Returns User handle.
        """
        return self.user.handle

    @property
    def user(self):
        """
        User record of session.
        """
        if self._user is not None:
            return self._user
        return x84.bbs.userbase.User()

    @user.setter
    def user(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        self._user = value
        logger = logging.getLogger()
        logger.info('user = %r', value.handle)
        if self.is_recording and self._ttyrec_fname != value.handle:
            # mv None.0 -> userName.0
            self.rename_recording(self._ttyrec_fname, value.handle)

    @property
    def source(self):
        """
        A string describing the session source
        """
        return '%s' % (self._source,)

    @source.setter
    def source(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        self._source = value

    @property
    def encoding(self):
        """
        Session terminal encoding; only 'utf8' and 'cp437' are supported.
        """
        return self._encoding

    @encoding.setter
    def encoding(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        if value != self._encoding:
            logger = logging.getLogger()
            logger.info('encoding=%s', value)
            assert value in ('utf8', 'cp437')
            self._encoding = value

    @property
    def enable_keycodes(self):
        """
        Translate multibyte sequences to single keycodes for input events.

        It may be desirable to temporarily disable this when doing
        pass-through, to another curses application such as a door.
        """
        return self._enable_keycodes

    @enable_keycodes.setter
    def enable_keycodes(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        if value != self._enable_keycodes:
            logger = logging.getLogger()
            logger.debug('enable_keycodes=%s', value)
            self._enable_keycodes = value

    @property
    def pid(self):
        """
        Returns Process ID.
        """
        # pylint: disable=R0201
        #        Method could be a function
        return os.getpid()

    def run(self):
        """
        Begin main execution flow.

        Scripts manipulate control flow of scripts using goto and gosub.
        """
        from x84.bbs.exception import Goto, Disconnected

        logger = logging.getLogger()

        def error_recovery():
            """
            jojo's invention; recover from a general exception by using
            a script stack, and resuming last good script.
            """
            if 0 != len(self._script_stack):
                # recover from exception
                fault = self._script_stack.pop()
                oper = 'RESUME' if len(self._script_stack) else 'STOP'
                stop = bool(0 == len(self._script_stack))
                msg = (u'%s %safter general exception in %s.' % (
                    oper, (self._script_stack[-1][0] + u' ')
                    if len(self._script_stack) else u' ', fault[0],))
                logger.info(msg)
                self.write(u'\r\n\r\n')
                if stop:
                    self.write(self.terminal.red_reverse('stop'))
                else:
                    self.write(self.terminal.bold_green('continue'))
                    self.write(u' ' + self.terminal.bold_cyan(
                        self._script_stack[-1][0]))
                self.write(u' after general exception in %s\r\n' % (
                    self.terminal.bold_cyan(fault[0]),))
                # give time for exception to write down pipe before
                # continuing or exiting, esp. exiting, otherwise
                # STOP message is not often fully received
                time.sleep(5)

        while len(self._script_stack):
            logger.debug('script_stack: %r', self._script_stack)
            try:
                return self.runscript(*self._script_stack.pop())
            except Goto, err:
                logger.debug('Goto: %s', err)
                self._script_stack = [err[0] + tuple(err[1:])]
                continue
            except Disconnected, err:
                break
            except Exception, err:
                # Pokemon exception, log and Cc: telnet client, then resume.
                e_type, e_value, e_tb = sys.exc_info()
                self.write(self.terminal.normal + u'\r\n')
                for line in traceback.format_tb(e_tb):
                    for subln in line.split('\n'):
                        logger.error(subln.rstrip())
                        self.write(subln.rstrip() + u'\r\n')
                for line in traceback.format_exception_only(e_type, e_value):
                    logger.error(line.rstrip())
                    self.write(self.terminal.bold_red(line.rstrip()) + u'\r\n')
                if not self.lock.acquire(False):
                    logger.error('session.lock forcefully unacquired')
                    self.lock = threading.Lock()
            error_recovery()
        self.close()
        return None

    def write(self, ucs):
        """
        Write unicode data to telnet client. Take special care to encode
        as 'iso8859-1' actually intended for 'cp437'-encoded terminals.

        Has side effect of updating ttyrec file when recording.
        """
        logger = logging.getLogger()
        if 0 == len(ucs):
            return
        assert isinstance(ucs, unicode)
        if self.encoding == 'cp437':
            encoding = 'iso8859-1'
            # out output terminal is cp437, so we need to take special care to
            # re-encode things as "iso8859-1" but really encoded for cp437.
            # For example, u'\u2591' becomes u'\xb0' (unichr(176)),
            # -- the original ansi shaded block for cp437 terminals.
            text = ucs.encode(encoding, 'replace')
            ucs = u''.join([(unichr(x84.bbs.cp437.CP437.index(glyph))
                             if glyph in x84.bbs.cp437.CP437
                             else unicode(text[idx], encoding, 'replace'))
                            for (idx, glyph) in enumerate(ucs)])
        else:
            encoding = self.encoding
        self.terminal.stream.write(ucs, encoding)

        if self._tap_output and logger.isEnabledFor(logging.DEBUG):
            logger.debug('--> %r.', ucs)

        if self._record_tty:
            if not self.is_recording:
                self.start_recording()
            self._ttyrec_write(ucs)

    def flush_event(self, event):
        """
        Flush all return all data buffered for 'event'.
        """
        logger = logging.getLogger()
        flushed = list()
        while True:
            data = self.read_event(event, timeout=-1)
            if data is None:
                if 0 != len(flushed):
                    logger.debug('flushed from %s: %r', event, flushed)
                return flushed
            flushed.append(data)
        return flushed

    def buffer_event(self, event, data=None):
        """
        Push data into buffer keyed by event. Allow only the most recent
        refresh event to be buffered.
        """
        # exceptions aren't buffered; they are thrown!
        if event == 'exception':
            # pylint: disable=E0702
            #        Raising NoneType while only classes, (..) allowed
            raise data

        # init new unmanaged & unlimited-sized buffer ;p
        if not event in self._buffer:
            self._buffer[event] = list()

        if event == 'input':
            self.buffer_input(data)
        elif event == 'refresh':
            if data[0] == 'resize':
                # inherit terminal dimensions values
                (self.terminal.columns, self.terminal.rows) = data[1]
            # store only most recent 'refresh' event
            self._buffer[event] = list((data,))
        else:
            self._buffer[event].insert(0, data)

    def buffer_input(self, data):
        """
        Update idle time, and encode input using session encoding such as
        'utf8' or 'cp437'. When enable_keycodes is True, yield single atoms
        for any detected multibyte sequences.

        The unfortunate side-effect is something might appear as an
        equivalent KEY_SEQUENCE that is better described as it was
        to traditional getch() users, '\r'(^J) and '\b'(^H) as
        term.KEY_ENTER and term.KEY_BACKSPACE, for example.

        When ^L/KEY_REFRESH in getch() stream as detected, a refresh event is
        buffered.
        """
        self._last_input_time = time.time()
        ctrl_l = self.terminal.KEY_REFRESH

        logger = logging.getLogger()
        if self._tap_input and logger.isEnabledFor(logging.DEBUG):
            logger.debug('<-- %r.', data)

        if not self.enable_keycodes:
            # send keyboard bytes in as-is, 1-by-1, unmanipulated
            for ch in data:
                self._buffer['input'].insert(0, ch)
            return

        # perform keycode translation with modified blessings/curses
        for keystroke in self.terminal.trans_input(data, self.encoding):
            if keystroke in (unichr(12), ctrl_l):
                self._buffer['input'].insert(0, ctrl_l)
                self._buffer['refresh'] = list((('input', ctrl_l),))
                continue
            self._buffer['input'].insert(0, keystroke)

    def send_event(self, event, data):
        """
           Send data to IPC pipe in form of (event, data).

           Supported events:
               'disconnect': Session wishes to disconnect.
               'logger': Data is logging record, used by IPCLogHandler.
               'output': Unicode data to write to client.
               'global': Broadcast event to other sessions.
               XX 'pos': Request cursor position.
               'db-<schema>': Request sqlite dict method result.
               'db=<schema>': Request sqlite dict method result as iterable.
               'lock-<name>': Fine-grained global bbs locking.
        """
        self.pipe.send((event, data))

    def poll_event(self, event):
        """
        Non-blocking poll for session event, returns value, if any. None
        otherwise.
        """
        return self.read_event(event, -1)

    def read_event(self, event, timeout=None):
        """
        S.read_event (event, timeout=None) --> data

        Read any data for a single event.

        Blocking by default, or non-blocking when timeout is -1. When timeout
        is non-zero, specifies length of time to wait for event before
        returning. If timeout is not None (non-blocking), None is returned if
        no event has is waiting, or waiting after timeout has elapsed.
        """
        return self.read_events(events=(event,), timeout=timeout)[1]

    def read_events(self, events, timeout=None):
        """
           S.read_events (events, timeout=None) --> (event, data)

           Return the first matched IPC data for any event specified in tuple
           events, in the form of (event, data).
        """
        logger = logging.getLogger()
        (event, data) = (None, None)
        # return immediately any events that are already buffered
        for (event, data) in ((e, self._event_pop(e))
                              for e in events if e in self._buffer
                              and 0 != len(self._buffer[e])):
            return (event, data)
        stime = time.time()
        timeleft = lambda cmp_time: \
            float('inf') if timeout is None \
            else timeout - (time.time() - cmp_time)
        waitfor = timeleft(stime)
        while waitfor > 0:
            poll = None if waitfor == float('inf') else waitfor
            if self.pipe.poll(poll):
                event, data = self.pipe.recv()
                self.buffer_event(event, data)
                if event in events:
                    logger.debug('event %s caught.', (event,))
                    return (event, self._event_pop(event))
                else:
                    logger.debug('event %s buffered.', (event,))
            if timeout == -1:
                return (None, None)
            waitfor = timeleft(stime)
        return (None, None)

    def _event_pop(self, event):
        """
        S._event_pop (event) --> data

        Returns foremost item buffered for event.
        """
        return self._buffer[event].pop()

    def runscript(self, script_name, *args):
        """
        Execute the main() callable of script identified by 'script_name', with
        optional *args.
        """
        logger = logging.getLogger()
        self._script_stack.append((script_name,) + args)
        logger.info('RUN %s%s', script_name,
                    '%r' % (args,) if 0 != len(args) else '')

        def _load_script_module():
            """
            Load and return ini folder, `scriptpath` as a module (cached).
            """
            if self._script_module is None:
                # load default/__init__.py as 'default',
                script_path = x84.bbs.ini.CFG.get('system', 'scriptpath')
                base_script = os.path.basename(script_path)
                lookup = imp.find_module(script_name, [script_path])
                self._script_module = imp.load_module(base_script, *lookup)
                self._script_module.__path__ = script_path
            return self._script_module
        script_module = _load_script_module()
        # pylint: disable=W0142
        #        Used * or ** magic
        lookup = imp.find_module(script_name, [script_module.__path__])
        script = imp.load_module(script_name, *lookup)
        if not hasattr(script, 'main'):
            raise x84.bbs.exception.ScriptError(
                "%s: main() not found." % (script_name,))
        if not callable(script.main):
            raise x84.bbs.exception.ScriptError(
                "%s: main not callable." % (script_name,))
        value = script.main(*args)
        toss = self._script_stack.pop()
        logger.info('%s <== %s', value, toss)
        return value

    def close(self):
        """
        Close session.
        """
        if self.is_recording:
            self.stop_recording()

# TODO: Somehow move these to ttyrec.py to keep session.py terse;
# TODO: lol, also exceptions . shite ...
    def rename_recording(self, src, dst):
        """
        Rotate ttyrec recording keyed by dst to make way for dst.0 by renaming
        src.0 to dst.0; not really sure this is correct ^_*
        """
        # acquire tty recording lock
        self.lock.acquire()
        logger = logging.getLogger()
        while True:
            self.send_event('lock-ttyrec', ('acquire', 5.0))
            if self.read_event('lock-ttyrec'):
                break
            logger.warn('failed to acquire ttyrec lock')
            time.sleep(0.6)
        self.rotate_recordings(dst)
        os.rename(os.path.join(self._ttyrec_folder, '%s.0' % (src,)),
                  os.path.join(self._ttyrec_folder, '%s.0' % (dst,)))
        # release tty recording lock
        self.send_event('lock-ttyrec', ('release', None))
        self._ttyrec_fname = dst
        self.lock.release()

    def rotate_recordings(self, key):
        """
        Rotate any existing ttyrec files for key.
        """
        logger = logging.getLogger()
        # if .8 exists, move .8 to .9, obliterating .9;
        # if .7 exists, move .7 to .8, obliterating .8; (..repeat)
        # aren't there helper functions for this stuff? :p
        for n in range(TTYREC_ROTATE):
            src = os.path.join(self._ttyrec_folder,
                               '%s.%d' % (key, (TTYREC_ROTATE - 1) - (n)))
            dst = os.path.join(self._ttyrec_folder,
                               '%s.%d' % (key, (TTYREC_ROTATE - 1) - (n - 1)))
            if os.path.exists(src):
                os.rename(src, dst)
                logger.debug('mv %r -> %r', src, os.path.basename(dst))
        dst = os.path.join(self._ttyrec_folder, '%s.0' % (key,))
        assert TTYREC_ROTATE != 0 and not os.path.exists(dst), dst

    @property
    def is_recording(self):
        """
        True when session is being recorded to ttyrec file
        """
        return self._fp_ttyrec is not None

    def stop_recording(self):
        """
        Cease recording to ttyrec file (close).
        """
        assert self.is_recording
        self._fp_ttyrec.close()
        self._fp_ttyrec = None

    def start_recording(self, dst=None):
        """
        Begin recording to ttyrec file keyed by 'dst'. When 'dst' is None
        (default), use the handle of the current session.
        """
        logger = logging.getLogger()
        assert self._fp_ttyrec is None, ('already recording')
        if dst is None:
            dst = self.user.handle or 'None'
        self._ttyrec_fname = dst
        # acquire tty recording lock
        self.lock.acquire()
        while True:
            self.send_event('lock-ttyrec', ('acquire', 5.0))
            if self.read_event('lock-ttyrec'):
                break
            logger.warn('failed to acquire ttyrec lock')
            time.sleep(0.6)
        # rotate logfiles,
        self.rotate_recordings(self._ttyrec_fname)
        # open ttyrec logfile for writing
        filename = os.path.join(self._ttyrec_folder,
                                '%s.0' % (self._ttyrec_fname,))
        if not os.path.exists(self._ttyrec_folder):
            logger.info('creating ttyrec folder, %s.', self._ttyrec_folder)
            os.makedirs(self._ttyrec_folder)
        self._fp_ttyrec = io.open(filename, 'wb+')
        self._ttyrec_sec = -1
        self._recording = True
        self._ttyrec_write_header()
        logger.info('REC %s' % (filename,))
        # release tty recording lock
        self.send_event('lock-ttyrec', ('release', None))
        self.lock.release()

    def _ttyrec_write_header(self):
        """
        Write ttyrec header that identifies termianl height & width, and escape
        sequence to indicate UTF-8 mode.
        """
        (h, w) = self.terminal.height, self.terminal.width
        self._ttyrec_write(TTYREC_HEADER % (h, w,))
        # ESC %G activates UTF-8 with an unspecified implementation level from
        # ISO 2022 in a way that allows to go back to ISO 2022 again.
        self._ttyrec_write(unichr(27) + u'%G')

    def _ttyrec_write(self, ucs):
        """
        Update ttyrec stream with unicode bytes 'ucs'.
        """
        # write bytestring to ttyrec file packed as timed byte.
        # If the current timed byte is within TTYREC_UCOMPRESS
        # (default: 15,000 μsec), rewind stream and re-write the
        # 'length' portion, and append data to end of stream.
        # .. unfortuantely, this is not compatible with ttyplay -p,
        # so for the time being, it is disabled ..
        assert self._recording, 'call start_recording() first'
        timeKey = self.duration

        # Round down timeKey to nearest whole number,
        # use the remainder for microseconds. Upconvert,
        # constructing a (seconds, microseconds) pair.
        sec = math.floor(timeKey)
        usec = (timeKey - sec) * 1e+6
        sec, usec = int(sec), int(usec)

        def write_chunk(tm_sec, tm_usec, textlen, u_text):
            """
            Write new timechunk record,
              bytes (sec, usec, len(text), text.. )
            """
            # build & write,
            bp1 = struct.pack('<I', tm_sec) + struct.pack('<I', tm_usec)
            bp2 = struct.pack('<I', textlen)
            self._fp_ttyrec.write(bp1 + bp2 + u_text)
            # save (time,len) state for compression
            self._ttyrec_sec = tm_sec
            self._ttyrec_usec = tm_usec
            self._ttyrec_len_text = textlen
            self._fp_ttyrec.flush()
        text = ucs.encode('utf8', 'replace')
        len_text = len(text)
        # write full chunk and return, unless compression is used.
        # Also, when more than TTYREC_PADD has elapsed, first a
        # 0-lengthed padding is inserted for every TTYREC_PADD seconds.
        if(TTYREC_UCOMPRESS is None
           or sec != self._ttyrec_sec
           or usec - self._ttyrec_usec > TTYREC_UCOMPRESS):
            while sec > TTYREC_PADD:
                write_chunk(sec, usec, 0, bytes())
                sec -= TTYREC_PADD
            return write_chunk(sec, usec, len_text, text)

        # a sort of monkey patching (compression);
        #   1. rewind to last length byte
        last_bp2 = struct.pack('<I', self._ttyrec_len_text)
        new_bp2 = struct.pack('<I', self._ttyrec_len_text + len_text)
        self._fp_ttyrec.seek((self._ttyrec_len_text + len(last_bp2)) * -1, 2)
        #   2. re-write length byte
        self._fp_ttyrec.write(new_bp2)
        #   3. append additional text after existing chunk record
        self._fp_ttyrec.seek(self._ttyrec_len_text, 1)
        self._fp_ttyrec.write(text)
        self._ttyrec_len_text = self._ttyrec_len_text + len_text
        self._fp_ttyrec.flush()
