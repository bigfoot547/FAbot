import asyncio
import time
import re
import base64
import random
import traceback


class ParseError(BaseException):
    def __init__(self, desc, line, cursor):
        self.desc = desc
        self.line = line
        self.cursor = cursor


def parse_nuh(s):
    if not s: return None

    nuh = {}
    if '!' in s:
        nuh['nick'] = s[:s.find('!')]
        nuh['ident'] = s[s.find('!')+1:s.find('@')]
        nuh['host'] = s[s.find('@')+1:]
    else:
        nuh['nick'] = s
        nuh['ident'] = ''
        nuh['host'] = ''
    return nuh


def create_nuh(nuh):
    if nuh['ident'] != '':
        return f"{nuh['nick']}!{nuh['ident']}@{nuh['host']}"
    return nuh['nick']


class IRCLine:
    WHITESPACE = ' '
    TAGS_INDICATOR = '@'
    SOURCE_INDICATOR = ':'
    TAG_CLIENT_PREFIX = '+'
    TAG_SEPARATOR = ';'
    TAG_VALID_KEY_PATTERN = re.compile('[a-zA-Z\\d\\-]+')
    TAG_ESCAPE_CHARS = {':': ';', 's': ' ', '\\': '\\', 'r': '\r', 'n': '\n'}
    TAG_ESCAPE_CHARS_REV = {';': ':', ' ': 's', '\\': '\\', '\r': 'r', '\n': 'n'}
    VALID_VERB_PATTERN = re.compile('\\d{3}|[A-Za-z]+')  # lowercase letters aren't technically allowed

    def __init__(self, line=None, do_tags=True, tags=None, source=None, verb=None, params=None):
        if tags is None:
            tags = {}
        if params is None:
            params = []

        self._cursor = 0

        self.line = line
        self._do_tags = do_tags
        self.tags = tags
        self.source = parse_nuh(source)
        self.sourceraw = source
        self.verb = verb
        self.params = params

    def _parse_error(self, desc):
        return ParseError(desc, self.line, self._cursor)

    def _parse_complete(self):
        return self._cursor >= len(self.line)

    # technically there should not be a bunch of whitespace in a row, but IRC servers are weird and not necessarily
    # standards-compliant
    def _skip_whitespace(self):
        while not self._parse_complete():
            if self.line[self._cursor] != IRCLine.WHITESPACE:
                return
            self._cursor += 1

    def _parse_tags(self):
        self._skip_whitespace()
        if self._parse_complete(): raise self._parse_error("Line ended while parsing tags")

        if self.line[self._cursor] != IRCLine.TAGS_INDICATOR: return  # no tags here
        self._cursor += 1
        if self._parse_complete(): raise self._parse_error("Line ended while parsing first tag")

        while self._parse_tag(): pass

    def _parse_tag(self):
        tag = {'key': None, 'vendor': None, 'value': '', 'client': False}
        rawvendor = None

        def check_set_key(s):
            if not re.fullmatch(IRCLine.TAG_VALID_KEY_PATTERN, s):
                raise self._parse_error("Invalid tag key")
            tag['key'] = s

        def unescape_val(s):
            ret = ''
            escape = False
            for ch in s:
                if escape:
                    escape = False
                    if ch in IRCLine.TAG_ESCAPE_CHARS:
                        ret += IRCLine.TAG_ESCAPE_CHARS[ch]
                    else:
                        ret += ch
                elif ch == '\\':
                    escape = True
                else:
                    ret += ch
            return ret

        if self.line[self._cursor] == IRCLine.TAG_CLIENT_PREFIX:
            tag['client'] = True
            self._cursor += 1
            if self._parse_complete(): raise self._parse_error("Line ended while parsing tag key")

        somestr = ''
        while not self._parse_complete():
            curch = self.line[self._cursor]
            if tag['vendor'] is None and curch == '/':  # Vendor found
                if len(somestr) == 0: raise self._parse_error("Empty vendor found while parsing tag")
                rawvendor = somestr

                try:
                    tag['vendor'] = rawvendor.encode().decode('idna')
                except UnicodeError as uerr:
                    print(f'Failed parsing IDNA vendor: {uerr}')
                    tag['vendor'] = rawvendor  # TODO: Handle this properly
                somestr = ''
            elif tag['key'] is None and curch == '=':  # value found
                check_set_key(somestr)
                somestr = ''
            elif curch == ';' or curch == IRCLine.WHITESPACE:  # end of tag (or tags)
                if not tag['key']:
                    check_set_key(somestr)
                else:
                    tag['value'] = unescape_val(somestr)

                if rawvendor:
                    self.tags[f'{rawvendor}/{tag["key"]}'] = tag
                else:
                    self.tags[tag['key']] = tag
                self._cursor += 1
                if self._parse_complete(): raise self._parse_error("Line ended while parsing tag")
                return curch != IRCLine.WHITESPACE
            else:
                somestr += curch

            self._cursor += 1
        raise self._parse_error("Line ended while parsing tag")

    def _parse_source(self):
        source = ''
        while not self._parse_complete():
            curch = self.line[self._cursor]
            if curch == IRCLine.WHITESPACE:
                if len(source) == 0: raise self._parse_error("Message source is empty")
                self.source = parse_nuh(source)
                self.sourceraw = create_nuh(self.source)
                return
            source += curch
            self._cursor += 1
        raise self._parse_error("Line ended while parsing source")

    def _parse_verb(self):
        verb = ''
        while not self._parse_complete():
            curch = self.line[self._cursor]
            if curch == IRCLine.WHITESPACE:
                break
            verb += curch
            self._cursor += 1
        if not re.fullmatch(IRCLine.VALID_VERB_PATTERN, verb):
            raise self._parse_error("Invalid verb")

        self.verb = verb.upper()

    def _parse_params(self):
        param = ''
        while not self._parse_complete():
            curch = self.line[self._cursor]
            if curch == IRCLine.WHITESPACE:
                self.params.append(param)
                self._skip_whitespace()
                param = ''
                continue

            if curch == ':' and len(param) == 0:
                self.params.append(self.line[self._cursor + 1:])
                return
            param += curch

            self._cursor += 1

        if len(param) != 0 and not param.isspace():
            self.params.append(param)

    def parse(self):
        if len(self.line) == 0:
            raise self._parse_error("Line is empty")

        if self._do_tags: self._parse_tags()
        self._skip_whitespace()
        if self._parse_complete(): raise self._parse_error("Line ended while parsing source or verb")

        if self.line[self._cursor] == ':':
            self._cursor += 1
            if self._parse_complete(): raise self._parse_error("Line ended while beginning to parse source")
            self._parse_source()
            self._skip_whitespace()
            if self._parse_complete(): raise self._parse_error("Line ended after parsing source")

        self._parse_verb()
        self._skip_whitespace()
        if self._parse_complete(): return

        self._parse_params()
        self.line = None  # Allow this class to be used for 'sanitizing' lines

    def __str__(self):
        if self.line:
            return self.line

        ret = ''

        def tag_value_escape(s):
            esc = ''
            for ch in s:
                if ch in IRCLine.TAG_ESCAPE_CHARS_REV:
                    esc += '\\'
                    esc += IRCLine.TAG_ESCAPE_CHARS_REV[ch]
                else:
                    esc += ch
            return esc

        if self.tags:
            first = True
            for tagname in self.tags:
                if first:
                    first = False
                    ret += '@'
                else:
                    ret += ';'

                tag = self.tags[tagname]
                if tag['client']:
                    ret += IRCLine.TAG_CLIENT_PREFIX
                if tag['vendor']:
                    ret += tag['vendor'].encode('idna').decode('utf-8', errors='replace')
                    ret += '/'

                if not re.fullmatch(IRCLine.TAG_VALID_KEY_PATTERN, tag['key']):
                    raise ValueError("Invalid tag key")

                ret += tag['key']
                if tag['value']:
                    ret += '='
                    ret += tag_value_escape(tag['value'])
            ret += ' '

        if self.sourceraw:
            ret += ':'
            ret += self.sourceraw
            ret += ' '

        if not re.fullmatch(IRCLine.VALID_VERB_PATTERN, self.verb):
            raise ValueError("Invalid verb")
        ret += self.verb

        trailing = False
        for param in self.params:
            if trailing: raise ValueError("Unable to have arguments after trailing argument")
            ret += ' '
            if IRCLine.WHITESPACE in param or len(param) == 0:
                trailing = True
                ret += ':'
            ret += param
        self.line = ret
        return ret


PING_FREQ_SECS = 30
PING_TIMEOUT_SECS = 60
SEEK_NICK_CHECK_FREQ = 20


# TODO: does not handle casemapping AT ALL (assumes ascii)
class IRCBot:
    def __init__(self, nick='ircbot', ident='unknown', realname='realname'):
        self.nick = nick
        self.account = None
        self.ident = ident
        self.host = 'unknown'
        self.realname = realname
        self.registered = False

        self._writer = None
        self._shutdown = False

        self._last_ping = None
        self._ping_key = None

        self._seek_nick = nick
        self._seek_nick_check = time.time()

        self.pending_responses = {}

    async def handle_raw_line(self, recv):
        line = IRCLine(recv)
        try:
            line.parse()
            print(f" IN: {line}")

            try:
                method = getattr(self, "handle_verb_" + line.verb.lower())
                await method(line)
            except AttributeError:
                await self.handle_unknown_verb(line)
            except BaseException as ex:
                print(f"Error when handling line '{line}'")
                traceback.print_exception(type(ex), ex, ex.__traceback__)
        except ParseError as perr:
            print(f"Message parser error encountered, disconnecting: '{perr.desc}' (at index {perr.cursor})")
            print(f"Line in question: '{perr.line}'")
            await self.quit("Invalid message received")

    async def handle_unknown_verb(self, line):
        #print(f"Line has unknown verb: {line}")
        pass

    async def write_line(self, line: IRCLine):
        print(f"OUT: {line}")
        self._writer.write((str(line) + "\r\n").encode('utf-8', errors='replace'))
        await self._writer.drain()

    async def on_connect(self):
        await self.register()

    async def quit(self, message: str):
        await self.write_line(IRCLine(verb="QUIT", params=[message]))

    async def join(self, channel, key=None):
        params = [channel]
        if key is not None: params.append(key)
        await self.write_line(IRCLine(verb='JOIN', params=params))

    def shutdown(self):
        self._shutdown = True

    async def register(self):
        await self.write_line(IRCLine(verb="NICK", params=[self.nick]))
        await self.write_line(IRCLine(verb="USER", params=[self.ident, '0', '*', self.realname]))

    async def on_register(self):
        self.registered = True

    async def add_event(self, name: str, data: str):
        evt = asyncio.Event()
        if name in self.pending_responses:
            if data in self.pending_responses[name]:
                self.pending_responses[name][data].append(evt)
            else:
                self.pending_responses[name][data] = [evt]
        else:
            self.pending_responses[name] = {data: [evt]}
        await evt.wait()

    def event_complete(self, name: str, data: str):
        if name in self.pending_responses:
            if data in self.pending_responses[name]:
                for evt in self.pending_responses[name][data]:
                    evt.set()
                self.pending_responses[name][data].clear()

    async def handle_verb_001(self, line):
        await self.on_register()

    async def handle_verb_303(self, line):
        online = line.params[1]
        if self.nick != self._seek_nick and self._seek_nick not in online.split(' '):
            await self.write_line(IRCLine(verb='NICK', params=[self._seek_nick]))

    async def handle_verb_396(self, line):
        self.host = line.params[1]

    async def handle_verb_433(self, line):
        if not self.registered:
            self.nick += '_'
            await self.write_line(IRCLine(verb='NICK', params=[self.nick]))

    async def handle_verb_900(self, line):
        nuh = line.params[1]
        self.account = line.params[2]
        self.nick = nuh[:nuh.find('!')]
        self.ident = nuh[nuh.find('!')+1:nuh.find('@')]
        self.host = nuh[nuh.find('@')+1:]

    async def handle_verb_901(self, line):
        self.account = None

    async def handle_verb_nick(self, line):
        if self.nick == line.source['nick']:
            self.nick = line.params[0]

    async def handle_verb_quit(self, line):
        if self.nick != self._seek_nick and self._seek_nick == line.source['nick']:
            await self.write_line(IRCLine(verb='NICK', params=[self._seek_nick]))

    async def handle_verb_ping(self, line):
        await self.write_line(IRCLine(verb='PONG', params=line.params))

    async def handle_verb_pong(self, line):
        if self._ping_key is not None and line.params[len(line.params)-1] == self._ping_key:
            self._ping_key = None

    async def tick_client(self):
        if not self.registered: return

        now = time.time()

        if self._ping_key is None:
            if self._last_ping is None or now - self._last_ping > PING_FREQ_SECS:
                self._ping_key = ("00000000" + hex(random.randint(0, 0x7fffffff))[2:])[-8:]
                self._last_ping = now
                await self.write_line(IRCLine(verb='PING', params=[self._ping_key]))
        else:
            if self._last_ping is not None and now - self._last_ping > PING_TIMEOUT_SECS:
                await self.quit(f"No ping reply in {int(now - self._last_ping)} seconds")
                self.shutdown()

        if self.nick != self._seek_nick and now - self._seek_nick_check > SEEK_NICK_CHECK_FREQ:
            await self.write_line(IRCLine(verb='ISON', params=[self._seek_nick]))
            self._seek_nick_check = now

    async def connect(self, host, port, ssl=None):
        reader, self._writer = await asyncio.open_connection(host=host, port=port, ssl=ssl)

        try:
            await self.on_connect()

            partial = None
            while not reader.at_eof() and not self._shutdown:
                await self.tick_client()

                try:
                    data = await asyncio.wait_for(reader.read(16384), 5.0)
                    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)

                    for line in lines:
                        if partial:
                            line = partial + line
                            partial = None
                        elif not line.endswith("\r") and not line.endswith("\n"):
                            partial = line
                            continue

                        if line == '' or line.isspace(): continue
                        line = line.strip('\r\n')

                        await self.handle_raw_line(line)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._writer.close()


# Doesn't send CAP END, and will not ever finish registering on CAP 302 servers
class CapAwareIRCBot(IRCBot):
    def __init__(self, req_caps=None, **kwargs):
        super().__init__(**kwargs)

        if req_caps is None:
            req_caps = ['message-tags', 'extended-join', 'account-tag', 'cap-notify', 'multi-prefix']

        self.req_caps = req_caps

        self.server_caps = {}
        self.our_caps = {}

    async def register(self):
        await self.cap_ls(wait=False)
        await super().register()

    async def handle_verb_cap(self, line):
        subcmd = line.params[1]
        if subcmd == 'LS' or subcmd == 'NEW':  # they are replying to our CAP query
            caplist = line.params[len(line.params)-1].split(' ')
            caps = {}
            for capspec in caplist:
                capsplt = capspec.split('=', maxsplit=1)
                val = ''
                if len(capsplt) == 2:
                    val = capsplt[1]
                caps[capsplt[0]] = val
            if subcmd == 'LS': await self.on_cap_ls(caps)
            else: await self.on_cap_new(caps)

            if len(line.params) == 3:
                self.event_complete('cap_ls', '*')
        elif subcmd == 'ACK':
            caps = line.params[2].split(' ')
            for cap in caps:
                if cap[0] == '-':
                    cap = cap[1:]
                    if cap in self.our_caps:
                        self.our_caps.pop(cap)
                else:
                    self.our_caps[cap] = True
                await self.on_cap_ack(cap)
        elif subcmd == 'NAK':
            caps = line.params[2].split(' ')
            for cap in caps:
                await self.on_cap_nak(cap)
        elif subcmd == 'DEL':
            caps = line.params[2].split(' ')
            await self.on_cap_del(caps)

    async def cap_ls(self, wait=True):
        self.server_caps.clear()
        await self.write_line(IRCLine(verb='CAP', params=['LS', '302']))
        if wait:
            await self.add_event('cap_ls', '*')

    async def cap_req(self, caps: [str], wait=True):
        # FIXME: prevent from going over length and from requesting empty list
        await self.write_line(IRCLine(verb='CAP', params=['REQ', ' '.join(caps)]))
        if wait:
            coros = []
            for cap in caps:
                coros.append(self.add_event('cap_req', cap))
            await asyncio.gather(*coros)

    async def cap_end(self, wait=True):
        await self.write_line(IRCLine(verb='CAP', params=['END']))

    async def on_cap_ls(self, caps: {}):
        to_req = []
        for cap in caps:
            self.server_caps[cap] = caps[cap]
            if cap in self.req_caps and cap not in self.our_caps:
                to_req.append(cap)
        if len(to_req) > 0:
            await self.cap_req(to_req, wait=False)

    async def on_cap_ack(self, name: str):
        self.event_complete('cap_req', name)

    async def on_cap_nak(self, name: str):
        self.event_complete('cap_req', name)

    async def on_cap_new(self, caps: {}):
        await self.on_cap_ls(caps)

    async def on_cap_del(self, caps: [str]):
        for cap in caps:
            if cap in self.our_caps:
                self.our_caps.pop(cap)
            if cap in self.server_caps:
                self.server_caps.pop(cap)


class SASLIRCBot(CapAwareIRCBot):
    def __init__(self, sasl_uname, sasl_pass, require_auth=False, **kwargs):
        super().__init__(**kwargs)

        self.req_caps.append("sasl")
        self.sasl_auth = (sasl_uname, sasl_pass)
        self.require_auth = require_auth

    async def on_cap_ack(self, name: str):
        await super(SASLIRCBot, self).on_cap_ack(name)
        if name == 'sasl':
            mechs = self.server_caps['sasl'].split(',')
            if 'PLAIN' not in mechs and self.require_auth:
                await self.quit("SASL PLAIN not supported")
                return
            await self.write_line(IRCLine(verb='AUTHENTICATE', params=['PLAIN']))

    async def handle_verb_authenticate(self, line):
        if line.params[0] == '+':
            namebytes = self.sasl_auth[0].encode('utf-8', errors='replace')
            passbytes = self.sasl_auth[1].encode('utf-8', errors='replace')
            auth = namebytes + b'\x00' + namebytes + b'\x00' + passbytes
            await self.write_line(IRCLine(verb='AUTHENTICATE', params=[base64.b64encode(auth).decode('utf-8', errors='replace')]))

    async def sasl_error(self):
        if self.require_auth:
            await self.quit("Not authenticated but require_auth is enabled")
        else:
            await self.cap_end()

    async def handle_verb_901(self, line):
        await super().handle_verb_901(line)
        await self.sasl_error()

    async def handle_verb_902(self, line):
        await self.sasl_error()

    async def handle_verb_903(self, line):
        await self.cap_end()

    async def handle_verb_904(self, line):
        await self.sasl_error()

    async def handle_verb_905(self, line):
        await self.sasl_error()

    async def handle_verb_906(self, line):
        await self.sasl_error()

    async def handle_verb_907(self, line):
        await self.sasl_error()

    async def handle_verb_908(self, line):
        await self.sasl_error()
